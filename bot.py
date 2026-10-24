
import asyncio
import logging
import os
import re
import threading
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from aiogram import Bot, Dispatcher
from aiogram.enums import ChatType
from aiogram.types import Message
from ddgs import DDGS
from supabase import create_client


# ============================================================
# НАСТРОЙКИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

CHAT_MODEL = os.getenv("CHAT_MODEL", "openai/gpt-oss-20b")
SMART_MODEL = os.getenv("SMART_MODEL", CHAT_MODEL)

MAX_HISTORY = 16
MAX_SHEETS_PER_CHAT = 50
MAX_MESSAGE_LENGTH = 12000
TELEGRAM_MESSAGE_LIMIT = 4000

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не задана переменная TELEGRAM_TOKEN")

if not GROQ_API_KEY:
    raise RuntimeError("Не задана переменная GROQ_API_KEY")


bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

supabase = None

if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        supabase = create_client(
            SUPABASE_URL,
            SUPABASE_SERVICE_ROLE_KEY,
        )
        logging.info("Supabase подключён")
    except Exception:
        logging.exception("Не удалось подключить Supabase")


# История диалогов отдельно для каждого чата и темы.
chat_history = defaultdict(lambda: deque(maxlen=MAX_HISTORY))

# Временное хранилище анкет в оперативной памяти.
# При перезапуске приложения оно очищается.
chat_sheets = defaultdict(list)


def get_scope(message: Message):
    """Возвращает ключ чата с учётом темы Telegram."""
    topic_id = getattr(message, "message_thread_id", None)
    return message.chat.id, topic_id


# ============================================================
# ХАРАКТЕР МАКА
# ============================================================

MAK_PERSONA = """
Ты — МАК, дружелюбный участник сообщества Dandy's World RP.

Общайся на русском языке, естественно и по-человечески.
Можно использовать разговорный стиль, шутки и лёгкий юмор.
Не превращай каждое сообщение в огромную статью.

Ты помогаешь с:
- обсуждением идей для RP;
- персонажами и их анкетами;
- правилами Dandy's World RP;
- разработкой проектов;
- поиском информации в интернете.

Если пользователь просит проверить анкету, проверяй её по правилам
из предоставленной базы знаний и инструкциям проверки.

Не выдумывай правила, которых нет в источниках.
Если информации недостаточно, прямо скажи об этом.

Слово «анкета» в контексте Dandy's World RP означает анкету
RP-персонажа, а не форму Google.

Не утверждай, что ты сохранил анкету навсегда:
в текущей реализации анкеты хранятся только до перезапуска бота.
"""


# ============================================================
# ПРОВЕРКА АНКЕТ
# ============================================================

CHECK_PROMPT = """
Ты — строгий проверяющий анкет Dandy's World RP.

Главное правило: используй правила из базы знаний.
Не придумывай новые правила и не изменяй правила самостоятельно.

ПРАВИЛА ПРОВЕРКИ:

- Обычный Toon: сумма пяти основных характеристик должна быть 15 звёзд.
- Main Toon и Lethal: сумма пяти основных характеристик должна быть
  16 звёзд.
- Каждая из пяти основных характеристик должна иметь от 1 до 5 звёзд.
- ХП: от 1 до 4 сердец. ХП не входит в сумму звёзд.
- Скиллчек не включай в сумму, если правила RP прямо указывают,
  что он не относится к пяти основным характеристикам.
- Не считай одну характеристику дважды.
- Если правила базы знаний уточняют эти ограничения, следуй базе знаний.
- Если неясно, к какой категории относится персонаж, попроси уточнение.
- Не выдумывай нарушения и не объявляй анкету правильной,
  если данные для проверки отсутствуют.
- Не говори, что достаточно уменьшить одну характеристику,
  если итоговая сумма всё ещё не соответствует правилам.
- Если нарушений нет, напиши:
  «Красавчик, всё по красоте, правда, это у анкетолога спроси)»

ВАЖНО ПРО СУММУ:

Сначала определи ровно пять основных характеристик.
Покажи их названия и значения.
Отдельно сложи эти пять значений.
Не включай ХП в сумму.
Не включай дополнительные характеристики, если правила не считают
их частью пяти основных характеристик.

Не путай условие «сумма должна быть ровно 15» с условием «сумма
должна быть не больше 15». Если правило говорит ровно 15, сумма
должна быть ровно 15. Аналогично для 16.

Если в анкете указана характеристика выше 5, это отдельное нарушение.
При этом не изменяй исходные данные молча и не считай исправленное
значение вместо исходного.

ФОРМАТ ОТВЕТА:

- Пиши обычным текстом, без Markdown-разметки.
- Не используй таблицы, символы **, обратные кавычки и заголовки #.
- Используй короткие пункты и обычные тире.
- Сначала напиши итог проверки.
- Затем перечисли нарушения и объясни каждое.
- После этого предложи конкретные исправления, если они возможны.
- Покажи арифметику суммы.
- Если данных недостаточно, задай уточняющий вопрос.
"""


# ============================================================
# ОЧИСТКА ФОРМАТИРОВАНИЯ ДЛЯ TELEGRAM
# ============================================================

def clean_telegram_text(text: str) -> str:
    """Преобразует распространённую Markdown-разметку в обычный текст."""

    if not text:
        return ""

    result = []

    for line in text.splitlines():
        stripped = line.strip()

        # Превращаем Markdown-таблицы в обычные строки.
        if "|" in stripped:
            cells = [
                cell.strip()
                for cell in stripped.strip("|").split("|")
            ]

            # Убираем строку-разделитель таблицы.
            if cells and all(
                cell and all(ch in "-: " for ch in cell)
                for cell in cells
            ):
                continue

            if cells:
                line = " — ".join(cells)

        result.append(line)

    text = "\n".join(result)

    # Убираем заголовки Markdown.
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)

    # Убираем жирный и подчёркнутый Markdown.
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.S)

    # Убираем курсив.
    text = re.sub(r"(?<!\w)\*(.*?)\*(?!\w)", r"\1", text, flags=re.S)
    text = re.sub(r"(?<!\w)_(.*?)_(?!\w)", r"\1", text, flags=re.S)

    # Убираем обратные кавычки.
    text = text.replace("`", "")

    # Убираем Markdown-ссылки, сохраняя текст ссылки.
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 — \2", text)

    return text.strip()


async def send_long_message(message: Message, text: str):
    """Отправляет длинный ответ несколькими сообщениями."""

    text = clean_telegram_text(text)

    if not text:
        text = "Что-то я не смог сформировать ответ. Попробуй ещё раз."

    while text:
        if len(text) <= TELEGRAM_MESSAGE_LIMIT:
            await message.answer(text)
            break

        split_at = text.rfind("\n", 0, TELEGRAM_MESSAGE_LIMIT)

        if split_at < TELEGRAM_MESSAGE_LIMIT // 2:
            split_at = TELEGRAM_MESSAGE_LIMIT

        chunk = text[:split_at].strip()

        if chunk:
            await message.answer(chunk)

        text = text[split_at:].strip()


# ============================================================
# БАЗА ЗНАНИЙ SUPABASE
# ============================================================

def load_knowledge():
    """Загружает правила RP из таблицы rp_knowledge."""

    if supabase is None:
        return []

    try:
        response = (
            supabase.table("rp_knowledge")
            .select("title,content")
            .limit(100)
            .execute()
        )

        return response.data or []

    except Exception:
        logging.exception("Ошибка загрузки базы знаний")
        return []


def get_knowledge_context(query: str, all_rules: bool = False) -> str:
    """Возвращает подходящие правила из Supabase."""

    rows = load_knowledge()

    if not rows:
        return "База знаний Supabase сейчас недоступна или пуста."

    if all_rules:
        selected = rows
    else:
        query_words = set(
            re.findall(r"[a-zа-яё0-9]+", query.lower())
        )

        scored = []

        for row in rows:
            title = str(row.get("title", ""))
            content = str(row.get("content", ""))

            searchable = f"{title} {content}".lower()

            score = sum(
                1 for word in query_words
                if len(word) > 2 and word in searchable
            )

            if score > 0:
                scored.append((score, row))

        scored.sort(key=lambda item: item[0], reverse=True)

        selected = [row for _, row in scored[:10]]

        if not selected:
            selected = rows[:5]

    parts = []

    for row in selected:
        title = row.get("title", "Правило")
        content = row.get("content", "")

        parts.append(f"{title}\n{content}")

    return "\n\n".join(parts)


# ============================================================
# GROQ — ИСКУССТВЕННЫЙ ИНТЕЛЛЕКТ
# ============================================================

def ask_ai_sync(messages, model=None):
    """Синхронный запрос к Groq."""

    if not GROQ_API_KEY:
        return "Ошибка: не задан GROQ_API_KEY."

    payload = {
        "model": model or CHAT_MODEL,
        "messages": messages,
        "temperature": 0.5,
        "max_tokens": 2500,
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            GROQ_URL,
            headers=headers,
            json=payload,
            timeout=90,
        )

        if not response.ok:
            logging.error(
                "Groq API вернул %s: %s",
                response.status_code,
                response.text[:1000],
            )
            return (
                "Не получилось получить ответ от ИИ. "
                "Попробуй немного позже."
            )

        data = response.json()

        return (
            data["choices"][0]["message"]["content"]
            or "ИИ вернул пустой ответ."
        )

    except Exception:
        logging.exception("Ошибка запроса к Groq")
        return "Произошла ошибка при обращении к ИИ."


async def ask_ai(messages, model=None):
    return await asyncio.to_thread(
        ask_ai_sync,
        messages,
        model,
    )


# ============================================================
# ИНТЕРНЕТ-ПОИСК
# ============================================================

def search_web_sync(query: str) -> str:
    """Ищет информацию в интернете через DDGS."""

    try:
        with DDGS() as ddgs:
            results = list(
                ddgs.text(query, max_results=5)
            )

        if not results:
            return "По этому запросу ничего найти не удалось."

        parts = []

        for item in results:
            title = item.get("title", "Без названия")
            body = item.get("body", "")
            url = item.get("href", "")

            parts.append(
                f"{title}\n{body}\n{url}"
            )

        return "\n\n".join(parts)

    except Exception:
        logging.exception("Ошибка интернет-поиска")
        return "Интернет-поиск временно недоступен."


async def answer_from_web(query: str) -> str:
    results = await asyncio.to_thread(search_web_sync, query)

    messages = [
        {
            "role": "system",
            "content": (
                MAK_PERSONA
                + "\nОтветь на вопрос по найденным результатам. "
                "Не выдавай неподтверждённые сведения за факты. "
                "Если есть ссылки, сохрани полезные ссылки в ответе."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Запрос пользователя: {query}\n\n"
                f"Результаты поиска:\n{results}"
            ),
        },
    ]

    return await ask_ai(messages)


# ============================================================
# ОБНАРУЖЕНИЕ И ПОИСК АНКЕТ
# ============================================================

def looks_like_sheet(text: str) -> bool:
    """Определяет, похоже ли сообщение на анкету персонажа."""

    if len(text) < 100:
        return False

    markers = (
        "имя:",
        "хп:",
        "hp:",
        "стамина:",
        "скорость:",
        "скрытность:",
        "экстракт:",
        "скиллчек:",
        "скилл чек:",
        "способность:",
        "способности:",
        "перезарядка:",
        "кд:",
        "toon",
        "мейн",
        "летал",
        "характеристики:",
        "характеристика:",
    )

    lowered = text.lower()

    found = sum(
        1 for marker in markers
        if marker in lowered
    )

    return found >= 2


def save_sheet(
    chat_id,
    topic_id,
    author_id,
    author_name,
    text,
):
    """Сохраняет анкету в оперативной памяти."""

    key = (chat_id, topic_id)

    sheet = {
        "author_id": author_id,
        "author_name": author_name or "Неизвестный автор",
        "text": text[:MAX_MESSAGE_LENGTH],
    }

    chat_sheets[key].append(sheet)

    if len(chat_sheets[key]) > MAX_SHEETS_PER_CHAT:
        chat_sheets[key] = chat_sheets[key][
            -MAX_SHEETS_PER_CHAT:
        ]

    logging.info(
        "Сохранена анкета в памяти: chat=%s author=%s",
        chat_id,
        author_id,
    )


def find_sheet(
    chat_id,
    topic_id,
    query="",
    requester_id=None,
):
    """Ищет сохранённую анкету в текущем чате и теме."""

    key = (chat_id, topic_id)
    sheets = chat_sheets.get(key, [])

    if not sheets:
        return None

    query = query.strip().lower()

    if query:
        # Сначала ищем совпадение по тексту или автору.
        for sheet in reversed(sheets):
            searchable = (
                sheet["text"] + " " + sheet["author_name"]
            ).lower()

            if query in searchable:
                return sheet

        # Дополнительный поиск по отдельным словам.
        query_words = [
            word for word in re.findall(r"[a-zа-яё0-9]+", query)
            if len(word) > 1
        ]

        if query_words:
            best_sheet = None
            best_score = 0

            for sheet in reversed(sheets):
                searchable = (
                    sheet["text"] + " " + sheet["author_name"]
                ).lower()

                score = sum(
                    1 for word in query_words
                    if word in searchable
                )

                if score > best_score:
                    best_score = score
                    best_sheet = sheet

            if best_score > 0:
                return best_sheet

        return None

    # Если запрос без имени — сначала ищем последнюю анкету
    # самого пользователя, затем последнюю анкету в чате.
    if requester_id is not None:
        for sheet in reversed(sheets):
            if sheet["author_id"] == requester_id:
                return sheet

    return sheets[-1]


def is_profile_question(query: str) -> bool:
    words = (
        "анкета",
        "характеристик",
        "стамина",
        "скиллчек",
        "скилл чек",
        "скорость",
        "скрытность",
        "экстракт",
        "хп",
        "сердц",
        "toon",
        "летал",
        "мейн",
        "персонаж",
        "перезаряд",
    )

    lowered = query.lower()

    return any(word in lowered for word in words)


# ============================================================
# ОБРАБОТКА СООБЩЕНИЙ
# ============================================================

@dp.message()
async def handle_messages(message: Message):
    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
        ChatType.PRIVATE,
    ):
        return

    text = (
        message.text
        or message.caption
        or ""
    ).strip()

    if not text:
        return

    scope = get_scope(message)
    chat_id, topic_id = scope

    # Обычные сообщения сохраняем в историю и проверяем,
    # не является ли сообщение анкетой.
    if not text.lower().startswith("макии"):
        chat_history[scope].append({
            "role": "user",
            "content": text[:4000],
        })

        if looks_like_sheet(text):
            user = message.from_user

            author_id = user.id if user else 0

            author_name = (
                user.full_name if user else "Неизвестный автор"
            )

            save_sheet(
                chat_id=chat_id,
                topic_id=topic_id,
                author_id=author_id,
                author_name=author_name,
                text=text,
            )

        return

    # Удаляем триггер «макии».
    command = text[len("макии"):].strip()
    command_lower = command.lower()

    if not command:
        await message.answer(
            "Йо! Что делаем, бро?"
        )
        return

    user = message.from_user
    requester_id = user.id if user else None

    # --------------------------------------------------------
    # МАКИИ НАЙДИ АНКЕТУ [ИМЯ ИЛИ ЧАСТЬ ТЕКСТА]
    # --------------------------------------------------------

    if command_lower.startswith("найди анкету"):
        query = command[len("найди анкету"):].strip()

        sheet = find_sheet(
            chat_id=chat_id,
            topic_id=topic_id,
            query=query,
            requester_id=requester_id,
        )

        if sheet is None:
            await message.answer(
                "Не нашёл такую анкету в сохранённых сообщениях. "
                "Проверь имя или отправь анкету ещё раз."
            )
            return

        result = (
            f"Нашёл анкету!\n"
            f"Автор: {sheet['author_name']}\n\n"
            f"{sheet['text']}"
        )

        await send_long_message(message, result)
        return

    # --------------------------------------------------------
    # МАКИИ ПРОВЕРЬ [АНКЕТУ]
    # --------------------------------------------------------

    if command_lower.startswith("проверь"):
        sheet_text = ""

        # Если команда является ответом на сообщение,
        # сначала проверяем текст того сообщения.
        replied = message.reply_to_message

        if replied:
            sheet_text = (
                replied.text
                or replied.caption
                or ""
            ).strip()

        # Если ответа на сообщение нет, ищем анкету.
        if not sheet_text:
            query = command[len("проверь"):].strip()

            sheet = find_sheet(
                chat_id=chat_id,
                topic_id=topic_id,
                query=query,
                requester_id=requester_id,
            )

            if sheet:
                sheet_text = sheet["text"]

        if not sheet_text or len(sheet_text) < 20:
            await message.answer(
                "Бро, не вижу анкету для проверки. "
                "Ответь на сообщение с анкетой командой "
                "«макии проверь» или сначала отправь анкету."
            )
            return

        await message.answer(
            "Проверяю анкету, бро..."
        )

        rules = await asyncio.to_thread(
            get_knowledge_context,
            sheet_text,
            True,
        )

        messages = [
            {
                "role": "system",
                "content": (
                    MAK_PERSONA
                    + "\n\n"
                    + CHECK_PROMPT
                    + "\n\nПРАВИЛА ИЗ БАЗЫ ЗНАНИЙ:\n"
                    + rules
                ),
            },
            {
                "role": "user",
                "content": (
                    "Проверь именно эту анкету. "
                    "Не меняй значения характеристик.\n\n"
                    + sheet_text
                ),
            },
        ]

        answer = await ask_ai(
            messages,
            model=SMART_MODEL,
        )

        await send_long_message(message, answer)
        return

    # --------------------------------------------------------
    # МАКИИ НАЙДИ [ПОИСК В ИНТЕРНЕТЕ]
    # --------------------------------------------------------

    if command_lower.startswith("найди"):
        query = command[len("найди"):].strip()

        if not query:
            await message.answer(
                "Что именно поискать в интернете?"
            )
            return

        await message.answer(
            "Ща поищу..."
        )

        answer = await answer_from_web(query)

        await send_long_message(message, answer)
        return

    # --------------------------------------------------------
    # ОБЫЧНЫЙ ДИАЛОГ С МАКОМ
    # --------------------------------------------------------

    rules = ""

    if is_profile_question(command):
        rules = await asyncio.to_thread(
            get_knowledge_context,
            command,
            False,
        )

    messages = [
        {
            "role": "system",
            "content": (
                MAK_PERSONA
                + (
                    "\n\nРЕЛЕВАНТНЫЕ ПРАВИЛА RP:\n" + rules
                    if rules else ""
                )
            ),
        }
    ]

    # Добавляем историю диалога.
    messages.extend(list(chat_history[scope]))

    # Добавляем текущий запрос.
    messages.append({
        "role": "user",
        "content": command,
    })

    answer = await ask_ai(messages)

    chat_history[scope].append({
        "role": "user",
        "content": command[:4000],
    })

    chat_history[scope].append({
        "role": "assistant",
        "content": answer[:4000],
    })

    await send_long_message(message, answer)


# ============================================================
# HTTP-СЕРВЕР ДЛЯ RENDER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"MAK bot is running!")

    def log_message(self, format, *args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        HealthHandler,
    )

    logging.info("HTTP health server запущен на порту %s", port)

    server.serve_forever()


# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    threading.Thread(
        target=start_health_server,
        daemon=True,
    ).start()

    logging.info("МАК запускается...")

    # Удаляем старый webhook, чтобы polling мог работать.
    await bot.delete_webhook(drop_pending_updates=False)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
