
import asyncio
import logging
import os
import threading
from collections import defaultdict, deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests
from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties

# ================= НАСТРОЙКИ =================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Экономичная модель для обычного общения
CHAT_MODEL = os.getenv(
    "CHAT_MODEL", "gemini-3.5-flash-lite"
)

# Модель для анкет и проверки баланса
SMART_MODEL = os.getenv(
    "SMART_MODEL", "gemini-3.6-flash"
)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models"

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задан TELEGRAM_TOKEN")

if not GEMINI_API_KEY:
    raise RuntimeError("Не задан GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("MAK")

bot = Bot(
    token=TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode=None),
)
dp = Dispatcher()

# История отдельно для каждого чата, максимум 30 сообщений
chat_history = defaultdict(lambda: deque(maxlen=30))


# ================= WEB-СЕРВЕР =================

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


# ================= GEMINI API =================

async def ask_gemini(system_prompt, user_prompt, model=CHAT_MODEL):
    url = f"{API_URL}/{model}:generateContent"

    payload = {
        "system_instruction": {
            "parts": [{"text": system_prompt}]
        },
        "contents": [{
            "role": "user",
            "parts": [{"text": user_prompt}]
        }],
        "generationConfig": {
            "temperature": 0.8,
            "maxOutputTokens": 1800,
        },
    }

    try:
        response = await asyncio.to_thread(
            requests.post,
            url,
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=45,
        )

        if response.status_code != 200:
            log.error(
                "Gemini API error: %s %s",
                response.status_code,
                response.text[:1000],
            )

            if response.status_code == 429:
                return "Бро, упёрлись в лимит Gemini 💀 Подожди немного и попробуй ещё раз."

            if response.status_code in (401, 403):
                return "Бро, Gemini отклонил доступ. Проверь API-ключ и доступ к модели."

            if response.status_code == 404:
                return (
                    f"Модель {model} не найдена или недоступна "
                    "для этого проекта. Проверь её в Google AI Studio."
                )

            return f"Ошибка Gemini API: HTTP {response.status_code}"

        data = response.json()
        candidates = data.get("candidates", [])

        if not candidates:
            feedback = data.get("promptFeedback", {})
            log.warning("Gemini returned no candidates: %s", feedback)
            return "Бро, Gemini не выдал ответ. Попробуй переформулировать запрос."

        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts", [])
        answer = "\n".join(
            part["text"]
            for part in parts
            if "text" in part
        ).strip()

        if not answer:
            return "Бро, Gemini вернул пустой ответ. Попробуй ещё раз."

        return answer

    except requests.Timeout:
        return "Бро, Gemini слишком долго отвечает. Попробуй ещё раз."
    except requests.RequestException:
        log.exception("Gemini connection error")
        return "Бро, не удалось подключиться к Gemini."
    except (ValueError, KeyError, TypeError):
        log.exception("Invalid Gemini response")
        return "Бро, пришёл ответ неожиданного формата от Gemini."


# ================= ОТПРАВКА ОТВЕТОВ =================

async def send_long_message(message, text):
    # Лимит Telegram — 4096 символов на сообщение.
    # Разбиваем чуть раньше, стараясь сохранить абзацы.
    while text:
        if len(text) <= 4000:
            chunk, text = text, ""
        else:
            split_at = text.rfind("\n", 0, 4000)
            if split_at < 1000:
                split_at = 4000
            chunk, text = text[:split_at], text[split_at:].lstrip()

        await message.answer(chunk)


# ================= ПРОМПТЫ =================

MAK_PERSONA = (
    "Ты МАК — умный, весёлый и дружелюбный бро. "
    "Общайся по-русски, неформально, используй уместный сленг, "
    "юмор и эмодзи. Отвечай точно. Девиз: "
    "'Сначала разберёмся, потом сделаем имбу'."
)

CHARACTER_PROMPT = MAK_PERSONA + """
Создай анкету персонажа для Dandy's World RP.
Учитывай историю чата, если она передана. Не выдумывай
реальные факты об участниках без оснований.

Правила:
1. Кто вы: любое существо, тун или монстр.
2. Возраст и адекватный рост обязательны.
3. Характер соответствует описанию или общению.
4. Друзей указывай только если есть основания.
5. Выбери тип: Тун, Мейн или Летальный.
6. Сумма пяти характеристик без ХП:
   Тун — строго 15 звёзд;
   Мейн — строго 16 звёзд;
   Летальный — строго 16 звёзд.
7. Скилл чек, скорость, выносливость, скрытность,
   экстракт: каждая характеристика от 1 до 5 звёзд.
8. ХП — от 1 до 4 сердец.
9. КД способности: Тун 60–100 сек.;
   Мейн 80–200 сек. Для Летального предложи разумный КД.
10. Выносливость: 1 звезда = 100 стамины;
    каждая следующая звезда добавляет 25% от 100;
    5 звёзд = 200 стамины.
11. Способность должна быть интересной, но не всемогущей.

Проверь сумму звёзд перед выдачей анкеты.

Формат:
🔥 АНКЕТА ПЕРСОНАЖА ОТ МАКа 🔥
👤 Имя / Кто вы:
🎂 Возраст:
📏 Рост:
🎭 Характер:
🤝 Друзья / С кем ладит:
🏷️ Тип:
⚡ Способность / Пассивка: (КД: ... сек)
📊 Характеристики (Всего звёзд: X/15 или X/16):
- ❤️ ХП: X/4 сердца
- 🎯 Скилл чек: X/5
- ⚡ Скорость: X/5
- 🏃 Выносливость: X/5 (X стамины)
- 👤 Скрытность: X/5
- 🛠️ Скорость экстракта: X/5
🎨 Внешность туна:
💬 Комментарий МАКа:
"""

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

    text_lower = text.lower()
    chat_id = message.chat.id
    author = message.from_user.first_name or "Участник"

    # Обычные сообщения только сохраняются для контекста.
    if not text_lower.startswith("макии"):
        chat_history[chat_id].append(f"{author}: {text[:1500]}")
        return

    # Убираем триггер "макии" и пробелы.
    command = text[5:].strip()
    command_lower = command.lower()

    if command_lower.startswith("найди"):
        query = command[5:].strip() or "придумай персонажа"

        history = "\n".join(chat_history[chat_id])
        if len(history) > 8000:
            history = history[-8000:]

        prompt = (
            f"Запрос: {query[:2000]}\n\n"
            f"История чата:\n{history or 'Чат пока пуст.'}"
        )

        status = await message.reply("🔍 МАК собирает анкету, бро...")
        result = await ask_gemini(
            CHARACTER_PROMPT,
            prompt,
            SMART_MODEL,
        )

        await status.delete()
        await send_long_message(message, result)
        return

    if command_lower.startswith("проверь"):
        query = command[7:].strip()

        if message.reply_to_message:
            replied_text = message.reply_to_message.text or ""
            if replied_text:
                query = f"{query}\n\nАнкета из сообщения:\n{replied_text}"

        if not query:
            await message.reply(
                "Бро, напиши анкету после «МакИИ проверь» "
                "или ответь реплаем на сообщение с анкетой!"
            )
            return

        status = await message.reply("🧐 МАК проверяет баланс...")
        result = await ask_gemini(
            CHECK_PROMPT,
            query[:12000],
            SMART_MODEL,
        )

        await status.delete()
        await send_long_message(message, result)
        return

    user_query = command or "Йо, бро, ты тут?"

    result = await ask_gemini(
        MAK_PERSONA,
        user_query[:8000],
        CHAT_MODEL,
    )

    await send_long_message(message, result)


async def main():
    log.info("Starting MAK bot")

    # Удаляем старый webhook для polling.
    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
