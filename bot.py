
import asyncio
import logging
import os
import re
import threading
from collections import defaultdict, deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from ddgs import DDGS
from supabase import create_client


# ============================================================
# НАСТРОЙКИ
# ============================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv(
    "SUPABASE_SERVICE_ROLE_KEY", ""
).strip()

CHAT_MODEL = os.getenv("CHAT_MODEL", "openai/gpt-oss-20b")
SMART_MODEL = os.getenv("SMART_MODEL", "openai/gpt-oss-20b")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

MAX_HISTORY = 30
MAX_SHEETS_PER_CHAT = 50
MAX_MESSAGE_LENGTH = 12000

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN")

if not GROQ_API_KEY:
    raise RuntimeError("Не задан GROQ_API_KEY")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("MAK")

bot = Bot(
    token=TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode=None),
)
dp = Dispatcher()

# История отдельно для каждого чата и темы Telegram.
chat_history = defaultdict(lambda: deque(maxlen=MAX_HISTORY))

# Временная память анкет: (chat_id, topic_id) -> список анкет.
chat_sheets = defaultdict(list)

# Не даём двум длинным запросам одновременно менять историю.
chat_locks = defaultdict(asyncio.Lock)


# ============================================================
# SUPABASE
# ============================================================

supabase = None

if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        supabase = create_client(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
        )
        log.info("Supabase подключён")
    except Exception:
        log.exception("Ошибка подключения к Supabase")
else:
    log.warning("Supabase не настроен")


def load_knowledge():
    """Загружает правила RP из таблицы rp_knowledge."""
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
        log.exception("Не удалось загрузить правила RP")
        return []


def get_knowledge_context(query, all_rules=False):
    """
    Для проверки анкеты all_rules=True передаёт все полученные правила.
    Для обычных вопросов пытается выбрать подходящие правила.
    """
    rows = load_knowledge()

    if not rows:
        return ""

    if all_rules:
        selected = rows
    else:
        query_lower = query.casefold()

        query_words = {
            word.strip(".,!?():;\"'«»")
            for word in query_lower.split()
            if len(word) >= 3
        }

        profile_question = is_profile_question(query)

        profile_keywords = (
            "анкет", "тун", "персонаж", "характеристик",
            "звезд", "звёзд", "хп", "стамин",
            "способност", "летальн", "мейн", "кд",
            "сердц", "рост", "экстракт",
        )

        ranked = []

        for row in rows:
            title = str(row.get("title", ""))
            content = str(row.get("content", ""))
            combined = f"{title}\n{content}"
            combined_lower = combined.casefold()

            score = 0

            for word in query_words:
                if word in combined_lower:
                    score += 2

            if profile_question and any(
                keyword in combined_lower
                for keyword in profile_keywords
            ):
                score += 10

            ranked.append((score, title, content))

        ranked.sort(key=lambda item: item[0], reverse=True)

        limit = 15 if profile_question else 6
        selected = [
            item for item in ranked if item[0] > 0
        ][:limit]

        if selected:
            selected = [
                {"title": title, "content": content}
                for _, title, content in selected
            ]
        else:
            selected = rows[:6]

    return "\n\n".join(
        f"Правило: {row.get('title', 'Без названия')}\n"
        f"{row.get('content', '')}"
        for row in selected
    )


# ============================================================
# ЛИЧНОСТЬ МАКА
# ============================================================

MAK_PERSONA = """
Ты МАК — дружелюбный, умный и весёлый бро.

Общайся на русском языке, неформально, используй уместный
сленг, юмор и эмодзи. Будь живым собеседником, но не превращай
каждый ответ в мем. Не жертвуй точностью ради шутки.

Если пользователь новичок, объясняй понятно и пошагово.
Если чего-то не знаешь, честно скажи об этом.
Не выдумывай факты и правила.

КОНТЕКСТ ПРОЕКТА:
Мы занимаемся Dandy's World RP.

Слово «анкета» по умолчанию означает анкету персонажа — туна
для нашего RP, а не Google Forms, опрос или регистрацию.

Если спрашивают, как сделать, заполнить или проверить анкету,
отвечай именно в контексте персонажей Dandy's World RP.
Используй переданные правила из Supabase.
Не придумывай неизвестные правила.

Если пользователь просит найти ранее отправленную анкету,
ориентируйся на текст анкеты, переданный в контексте запроса.
Если анкета не найдена, не делай вид, что нашёл её.

Девиз: «Сначала разберёмся, потом сделаем имбу».
"""


CHECK_PROMPT = MAK_PERSONA + """
Ты также проверяешь баланс анкет Dandy's World RP.

Используй правила из переданной базы RP.
Покажи расчёты и объясни каждое обнаруженное нарушение.

Известные правила проекта:
- Обычный Тун: сумма пяти характеристик равна 15 звёздам.
- Мейн и Летальный: сумма пяти характеристик равна 16 звёздам.
- Каждая характеристика от 1 до 5 звёзд.
- ХП от 1 до 4 сердец; ХП не входит в сумму звёзд.
- КД Туна: 60–100 секунд.
- КД Мейна: 80–200 секунд.
- 1 звезда выносливости = 100 стамины.
- Каждая дополнительная звезда добавляет 25 к базовым 100.
- 5 звёзд выносливости = 200 стамины.

Если база содержит дополнительные правила, учитывай и их.
Если правила базы противоречат известным выше, укажи противоречие.
Не выдумывай нарушения и не считай неизвестные поля ошибками.

Если нарушений нет, обязательно напиши:
«Красавчик все по красоте, правда это анкетолога то спроси)»
"""


# ============================================================
# РАСПОЗНАВАНИЕ АНКЕТ
# ============================================================

def is_profile_question(query):
    query = query.casefold()

    keywords = (
        "анкет", "тун", "туна", "туны", "персонаж",
        "характеристик", "звезд", "звёзд", "хп",
        "стамин", "способност", "летальн", "мейн",
        "main toon", "toon", "dandy's world",
    )

    return any(word in query for word in keywords)


def looks_like_sheet(text):
    """
    Эвристика для автоматического сохранения анкеты.
    Сообщение должно быть достаточно длинным и содержать
    несколько характерных полей.
    """
    lower = text.casefold()

    markers = (
        "имя:", "редкость:", "редкость",
        "хп:", "стамина:", "скорость:",
        "скрытность:", "скилл чек:",
        "скорость экстракта:", "способность:",
        "способности:", "кд:", "персонаж:",
        "выносливость:", "экстракт",
        "main toon", "toon",
    )

    count = sum(marker in lower for marker in markers)

    return len(text) >= 100 and count >= 2


def save_sheet(chat_id, topic_id, author_id, author_name, text):
    key = (chat_id, topic_id)

    chat_sheets[key].append({
        "author_id": author_id,
        "author_name": author_name,
        "text": text[:MAX_MESSAGE_LENGTH],
    })

    if len(chat_sheets[key]) > MAX_SHEETS_PER_CHAT:
        chat_sheets[key] = chat_sheets[key][-MAX_SHEETS_PER_CHAT:]


def find_sheet(chat_id, topic_id, query="", requester_id=None):
    sheets = chat_sheets.get((chat_id, topic_id), [])

    if not sheets:
        return None

    query = query.casefold().strip()

    # Ищем по имени автора или любому фрагменту текста анкеты.
    if query:
        matches = [
            sheet for sheet in sheets
            if query in sheet["text"].casefold()
            or query in sheet["author_name"].casefold()
        ]

        if matches:
            return matches[-1]

    # Без поискового запроса: сначала последняя анкета автора.
    if requester_id is not None:
        own = [
            sheet for sheet in sheets
            if sheet["author_id"] == requester_id
        ]

        if own:
            return own[-1]

    return sheets[-1]


# ============================================================
# GROQ API
# ============================================================

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
                "Groq HTTP %s: %s",
                response.status_code,
                response.text[:800],
            )

            if response.status_code == 429:
                return (
                    "Бро, мы упёрлись в лимит Groq 💀 "
                    "Попробуй немного позже."
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
            return "Бро, ИИ не выдал ответ. Попробуй снова."

        answer = (
            choices[0].get("message", {}).get("content") or ""
        ).strip()

        return answer or "Бро, ИИ вернул пустой ответ."

    except requests.Timeout:
        return "Бро, ИИ слишком долго отвечает. Попробуй ещё раз."
    except requests.RequestException:
        log.exception("Ошибка соединения с Groq")
        return "Бро, не удалось подключиться к ИИ."
    except (ValueError, KeyError, TypeError):
        log.exception("Некорректный ответ Groq")
        return "Бро, пришёл ответ неожиданного формата."


# ============================================================
# ИНТЕРНЕТ-ПОИСК
# ============================================================

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


async def answer_from_web(query):
    try:
        results = await asyncio.to_thread(search_web_sync, query)
    except Exception:
        log.exception("Ошибка интернет-поиска")
        return "Бро, интернет-поиск сейчас не сработал. Попробуй позже."

    if not results:
        return "Бро, ничего не нашёл. Попробуй сформулировать запрос иначе."

    sources = []

    for index, item in enumerate(results, start=1):
        sources.append(
            f"[{index}] {item['title']}\n"
            f"URL: {item['url']}\n"
            f"Описание: {item['body']}"
        )

    system_prompt = MAK_PERSONA + """
Отвечай на основе результатов интернет-поиска.
Не выдумывай факты или URL. Если источники противоречат друг другу,
скажи об этом. В конце добавь раздел «Источники».
"""

    prompt = (
        f"Запрос: {query}\n\n"
        f"Результаты поиска:\n\n" + "\n\n".join(sources)
    )

    return await ask_ai(system_prompt, prompt, SMART_MODEL)


# ============================================================
# ОТПРАВКА СООБЩЕНИЙ
# ============================================================

async def send_long_message(message, text):
    text = text or "Бро, ответ почему-то пустой."

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


# ============================================================
# ОБРАБОТКА TELEGRAM
# ============================================================

@dp.message()
async def handle_messages(message: types.Message):
    # Поддерживаем обычный текст и подписи к документам/фото.
    text = (message.text or message.caption or "").strip()

    if not text or not message.from_user:
        return

    chat_id = message.chat.id
    topic_id = message.message_thread_id or 0
    history_key = (chat_id, topic_id)

    author = message.from_user.first_name or "Участник"
    text_lower = text.casefold()

    # --------------------------------------------------------
    # ОБЫЧНЫЕ СООБЩЕНИЯ: СОХРАНЕНИЕ ИСТОРИИ И АНКЕТ
    # --------------------------------------------------------

    if not text_lower.startswith("макии"):
        chat_history[history_key].append(
            f"{author}: {text[:1500]}"
        )

        if looks_like_sheet(text):
            save_sheet(
                chat_id=chat_id,
                topic_id=topic_id,
                author_id=message.from_user.id,
                author_name=author,
                text=text,
            )
            log.info("Saved probable sheet in chat %s", chat_id)

        return

    command = text[5:].strip()
    command_lower = command.casefold()

    # --------------------------------------------------------
    # ПОИСК АНКЕТЫ
    # --------------------------------------------------------

    if command_lower.startswith("найди анкету"):
        query = command[len("найди анкету"):].strip()

        sheet = find_sheet(
            chat_id=chat_id,
            topic_id=topic_id,
            query=query,
            requester_id=message.from_user.id,
        )

        if not sheet:
            await message.reply(
                "Бро, в моей временной памяти пока нет анкет 😭\n\n"
                "Отправь анкету обычным текстовым сообщением. "
                "Я ищу анкеты по характерным полям и сохраняю "
                "подходящие сообщения. Затем попробуй снова."
            )
            return

        await send_long_message(
            message,
            "Нашёл! 🔎\n"
            f"Автор сообщения: {sheet['author_name']}\n\n"
            f"{sheet['text']}",
        )
        return

    # --------------------------------------------------------
    # ПРОВЕРКА АНКЕТЫ
    # --------------------------------------------------------

    if command_lower.startswith("проверь"):
        query = command[len("проверь"):].strip()

        # Если ответили на сообщение с анкетой — проверяем его.
        if message.reply_to_message:
            replied = (
                message.reply_to_message.text
                or message.reply_to_message.caption
                or ""
            ).strip()

            if replied:
                query = replied

        # Если текст анкеты не указан, пробуем найти сохранённую.
        if not query:
            sheet = find_sheet(
                chat_id=chat_id,
                topic_id=topic_id,
                requester_id=message.from_user.id,
            )

            if sheet:
                query = sheet["text"]

        if not query:
            await message.reply(
                "Бро, не вижу текст анкеты 😭\n"
                "Ответь командой «макии проверь» на сообщение "
                "с анкетой или отправь текст после команды."
            )
            return

        status = await message.reply("🧐 Проверяю анкету...")

        rules = await asyncio.to_thread(
            get_knowledge_context,
            query,
            True,
        )

        prompt = f"АНКЕТА ДЛЯ ПРОВЕРКИ:\n{query[:MAX_MESSAGE_LENGTH]}"

        if rules:
            prompt += f"\n\nВСЕ ПОЛУЧЕННЫЕ ПРАВИЛА ИЗ SUPABASE:\n{rules}"
        else:
            prompt += (
                "\n\nВНИМАНИЕ: правила Supabase недоступны. "
                "Не утверждай, что проверил анкету по всей базе."
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

    # --------------------------------------------------------
    # ИНТЕРНЕТ-ПОИСК
    # --------------------------------------------------------

    if command_lower.startswith("найди"):
        query = command[len("найди"):].strip()

        if not query:
            await message.reply(
                "Бро, что ищем? Например: "
                "«макии найди способности Astro Dandy's World»."
            )
            return

        status = await message.reply("🌐 Ищу в интернете...")

        result = await answer_from_web(query)

        try:
            await status.delete()
        except Exception:
            pass

        await send_long_message(message, result)
        return

    # --------------------------------------------------------
    # ОБЫЧНОЕ ОБЩЕНИЕ
    # --------------------------------------------------------

    user_query = command or "Йо, бро, ты тут?"

    history = "\n".join(chat_history[history_key])

    if len(history) > 6000:
        history = history[-6000:]

    rules = await asyncio.to_thread(
        get_knowledge_context,
        user_query,
    )

    prompt_parts = [
        f"Сообщение пользователя: {user_query[:6000]}"
    ]

    if history:
        prompt_parts.append(f"История чата:\n{history}")

    if rules:
        prompt_parts.append(f"Правила RP из Supabase:\n{rules}")

    if is_profile_question(user_query):
        prompt_parts.append(
            "Это вопрос об анкете персонажа Dandy's World RP. "
            "Не путай её с Google Forms. Используй правила выше."
        )

    result = await ask_ai(
        MAK_PERSONA,
        "\n\n".join(prompt_parts),
        CHAT_MODEL,
    )

    await send_long_message(message, result)

    chat_history[history_key].append(
        f"{author}: {user_query[:1500]}"
    )
    chat_history[history_key].append(
        f"МАК: {result[:1500]}"
    )


# ============================================================
# RENDER WEB SERVER
# ============================================================

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

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        SimpleHandler,
    )

    server.serve_forever()


threading.Thread(
    target=run_web_server,
    daemon=True,
).start()


# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    log.info("Starting MAK bot")

    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
