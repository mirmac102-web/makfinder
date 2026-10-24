
import asyncio
import logging
import os
import threading
from collections import defaultdict, deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from ddgs import DDGS
from supabase import create_client


# ================= НАСТРОЙКИ =================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv(
    "SUPABASE_SERVICE_ROLE_KEY", ""
).strip()

CHAT_MODEL = os.getenv(
    "CHAT_MODEL", "openai/gpt-oss-20b"
)
SMART_MODEL = os.getenv(
    "SMART_MODEL", "openai/gpt-oss-20b"
)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN")

if not GROQ_API_KEY:
    raise RuntimeError("Не задан GROQ_API_KEY")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("MAK")

bot = Bot(
    token=TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode=None),
)
dp = Dispatcher()

# История отдельно для каждого чата и каждой темы форума.
chat_history = defaultdict(lambda: deque(maxlen=30))

# Supabase необязателен для запуска бота, но нужен для правил RP.
supabase = None

if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        supabase = create_client(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
        )
        log.info("Supabase configured")
    except Exception:
        log.exception("Supabase initialization failed")
else:
    log.warning("Supabase credentials missing; database disabled")


# ================= WEB-СЕРВЕР RENDER =================

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"MAK Bot is running!"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def run_web_server():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()


threading.Thread(
    target=run_web_server,
    daemon=True,
).start()


# ================= GROQ ИИ =================

async def ask_ai(system_prompt, user_prompt, model=CHAT_MODEL):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 1800,
    }

    try:
        response = await asyncio.to_thread(
            requests.post,
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=45,
        )

        if response.status_code != 200:
            log.error(
                "Groq error: HTTP %s: %s",
                response.status_code,
                response.text[:800],
            )

            if response.status_code == 429:
                return (
                    "Бро, мы упёрлись в лимит Groq 💀 "
                    "Подожди немного и попробуй снова."
                )

            if response.status_code in (401, 403):
                return (
                    "Бро, Groq отклонил доступ. "
                    "Проверь GROQ_API_KEY в Render."
                )

            return f"Ошибка Groq API: HTTP {response.status_code}"

        data = response.json()
        choices = data.get("choices", [])

        if not choices:
            return "Бро, ИИ не выдал ответ. Попробуй ещё раз."

        answer = (
            choices[0].get("message", {}).get("content") or ""
        ).strip()

        return answer or "Бро, ИИ вернул пустой ответ."

    except requests.Timeout:
        return "Бро, ИИ долго отвечает. Попробуй ещё раз."
    except requests.RequestException:
        log.exception("Groq connection error")
        return "Бро, не удалось подключиться к Groq."
    except (ValueError, KeyError, TypeError):
        log.exception("Invalid Groq response")
        return "Бро, пришёл ответ неожиданного формата."


# ================= ПОИСК ПРАВИЛ В SUPABASE =================

def load_knowledge():
    if supabase is None:
        return []

    try:
        result = (
            supabase.table("rp_knowledge")
            .select("title,content")
            .limit(100)
            .execute()
        )
        return result.data or []
    except Exception:
        log.exception("Supabase knowledge query failed")
        return []


def get_knowledge_context(query):
    rows = load_knowledge()

    if not rows:
        return ""

    # Выбираем правила, совпадающие с запросом.
    words = {
        word.casefold()
        for word in query.split()
        if len(word) >= 3
    }

    ranked = []

    for row in rows:
        title = str(row.get("title", ""))
        content = str(row.get("content", ""))
        combined = f"{title} {content}"

        score = sum(
            1 for word in words if word in combined.casefold()
        )

        ranked.append((score, title, content))

    ranked.sort(key=lambda item: item[0], reverse=True)

    # При небольшом числе правил передаём все;
    # иначе — только несколько наиболее подходящих.
    selected = [
        item for item in ranked if item[0] > 0
    ][:6]

    if not selected:
        selected = ranked[:4]

    return "\n\n".join(
        f"Правило: {title}\n{content}"
        for _, title, content in selected
    )


# ================= ИНТЕРНЕТ-ПОИСК =================

def search_web_sync(query):
    results = []

    with DDGS() as ddgs:
        for item in ddgs.text(query, max_results=6):
            results.append({
                "title": item.get("title", "Без названия"),
                "url": item.get("href", ""),
                "body": item.get("body", ""),
            })

    return results


async def search_web(query):
    try:
        return await asyncio.to_thread(search_web_sync, query)
    except Exception:
        log.exception("Internet search failed")
        return []


async def answer_from_web(query):
    results = await search_web(query)

    if not results:
        return (
            "Бро, сейчас не получилось найти результаты в интернете. "
            "Попробуй ещё раз немного позже."
        )

    formatted_sources = []

    for index, item in enumerate(results, start=1):
        formatted_sources.append(
            f"[{index}] {item['title']}\n"
            f"URL: {item['url']}\n"
            f"Описание: {item['body']}"
        )

    sources_text = "\n\n".join(formatted_sources)

    system_prompt = (
        MAK_PERSONA
        + "\nТы отвечаешь на основе результатов интернет-поиска. "
        "Отделяй найденные сведения от предположений. "
        "Не утверждай, что источник подтверждает то, чего в нём нет. "
        "Если результаты противоречат друг другу, сообщи об этом. "
        "В конце добавь раздел «Источники» со ссылками из результатов. "
        "Не придумывай URL."
    )

    prompt = (
        f"Запрос пользователя: {query}\n\n"
        f"Результаты интернет-поиска:\n{sources_text}\n\n"
        "Ответь по-русски, кратко и полезно. "
        "Используй найденные ссылки в разделе источников."
    )

    answer = await ask_ai(system_prompt, prompt, SMART_MODEL)

    if answer.startswith("Ошибка Groq API:"):
        return answer

    return answer


# ================= ОТПРАВКА ОТВЕТОВ =================

async def send_long_message(message, text):
    while text:
        if len(text) <= 4000:
            chunk, text = text, ""
        else:
            split_at = text.rfind("\n", 0, 4000)
            if split_at < 1000:
                split_at = 4000

            chunk = text[:split_at]
            text = text[split_at:].lstrip()

        await message.answer(chunk)


# ================= ХАРАКТЕР МАКА =================

MAK_PERSONA = (
    "Ты МАК — умный, весёлый и дружелюбный бро. "
    "Общайся по-русски, неформально, используй уместный сленг, "
    "юмор и эмодзи. Не жертвуй точностью ради шутки. "
    "Девиз: «Сначала разберёмся, потом сделаем имбу»."
)


# ================= ПРОВЕРКА АНКЕТ =================

CHECK_PROMPT = MAK_PERSONA + """
Ты эксперт по балансу анкет Dandy's World RP.

Проверь присланную анкету:
1. Сложи только пять характеристик в звёздах:
   скилл чек + скорость + выносливость +
   скрытность + скорость экстракта.
2. Тун — ровно 15 звёзд; Мейн и Летальный —
   ровно 16 звёзд.
3. Каждая из пяти характеристик должна быть от 1 до 5.
4. ХП — от 1 до 4 сердец. ХП не входит в сумму звёзд.
5. КД: Тун 60–100 сек.; Мейн 80–200 сек.
6. Проверь рост, стамину и соответствие способности роли.
7. Не считай отсутствующую информацию автоматически ошибкой:
   отметь её как неизвестную.
8. Не выдумывай нарушения. Покажи арифметику суммы.

Если нарушений нет, обязательно напиши:
«Красавчик все по красоте, правда это анкетолога то спроси)»

Общайся с юмором и иронией, но проверяй математику внимательно.
"""


# ================= ОБРАБОТКА СООБЩЕНИЙ =================

@dp.message()
async def handle_messages(message: types.Message):
    text = (message.text or "").strip()

    if not text or not message.from_user:
        return

    text_lower = text.casefold()
    chat_id = message.chat.id
    topic_id = message.message_thread_id or 0
    history_key = (chat_id, topic_id)

    author = message.from_user.first_name or "Участник"

    # Сохраняем сообщения для контекста,
    # но бот не отвечает на каждое сообщение.
    if not text_lower.startswith("макии"):
        chat_history[history_key].append(
            f"{author}: {text[:1500]}"
        )
        return

    command = text[5:].strip()
    command_lower = command.casefold()

    # «МАКИИ найди» теперь выполняет интернет-поиск.
    if command_lower.startswith("найди"):
        query = command[5:].strip()

        if not query:
            await message.reply(
                "Бро, что именно ищем? Например: "
                "«макии найди способности Astro Dandy's World»."
            )
            return

        status = await message.reply(
            "🌐 Ищу в интернете, бро..."
        )

        try:
            result = await answer_from_web(query)
        except Exception:
            log.exception("Web answer failed")
            result = (
                "Бро, при поиске что-то сломалось. "
                "Попробуй ещё раз позже."
            )

        try:
            await status.delete()
        except Exception:
            pass

        await send_long_message(message, result)
        return

    # Проверка анкеты.
    if command_lower.startswith("проверь"):
        query = command[7:].strip()

        if message.reply_to_message:
            replied_text = (
                message.reply_to_message.text
                or message.reply_to_message.caption
                or ""
            )

            if replied_text:
                query = (
                    f"{query}\n\n"
                    f"Анкета из сообщения:\n{replied_text}"
                )

        if not query:
            await message.reply(
                "Бро, напиши анкету после «макии проверь» "
                "или ответь на сообщение с анкетой!"
            )
            return

        rules = await asyncio.to_thread(
            get_knowledge_context, query
        )

        prompt = f"Анкета для проверки:\n{query[:12000]}"

        if rules:
            prompt += (
                "\n\nПравила RP из базы данных Supabase:\n"
                f"{rules}"
            )

        status = await message.reply(
            "🧐 МАК проверяет баланс..."
        )

        result = await ask_ai(
            CHECK_PROMPT,
            prompt,
            SMART_MODEL,
        )

        try:
            await status.delete()
        except Exception:
            pass

        await send_long_message(message, result)
        return

    # Обычное общение.
    user_query = command or "Йо, бро, ты тут?"

    history = "\n".join(chat_history[history_key])

    if len(history) > 6000:
        history = history[-6000:]

    rules = await asyncio.to_thread(
        get_knowledge_context, user_query
    )

    prompt = f"Сообщение пользователя: {user_query[:6000]}"

    if history:
        prompt += f"\n\nНедавняя история этой темы:\n{history}"

    if rules:
        prompt += (
            "\n\nПолезные правила RP из Supabase:\n"
            f"{rules}"
        )

    result = await ask_ai(
        MAK_PERSONA,
        prompt,
        CHAT_MODEL,
    )

    await send_long_message(message, result)


# ================= ЗАПУСК =================

async def main():
    log.info("Starting MAK bot")

    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
