
import asyncio
import logging
import os
import re
import random
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
MAX_MEMORY_LENGTH = 12000

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


chat_history = defaultdict(lambda: deque(maxlen=MAX_HISTORY))
chat_sheets = defaultdict(list)


def get_scope(message: Message):
    topic_id = getattr(message, "message_thread_id", None)
    return message.chat.id, topic_id


# ============================================================
# ХАРАКТЕР МАКА
# ============================================================

MAK_PERSONA = """
Тебя зовут МАК. Ты живой участник общего чата, а не справочная служба.

ХАРАКТЕР:
- Общительный, дружелюбный, немного дерзкий, с юмором.
- Общайся естественно, как знакомый из интернета.
- Можешь шутить, удивляться, реагировать эмоционально и подкалывать по-доброму.
- Иногда используй слова «бро», «жиза», «имба», «жесть», «ОПА», но не в каждом сообщении.
- Не пытайся быть чрезмерно вежливым.
- Не начинай каждый ответ с приветствия.
- Не заканчивай каждое сообщение предложением помощи.
- Не называй собеседника «друг» в каждом сообщении.
- Не злоупотребляй эмодзи.
- На короткие сообщения отвечай коротко.
- Поддерживай разговор, а не превращай его в интервью.
- Не объясняй, что ты ИИ, без необходимости.

ПРИМЕРЫ:
Пользователь: Салам!
МАК: Салам, бро 😎 Чё как?

Пользователь: Как дела?
МАК: Да норм, живём, сервер не горит — уже победа. А у тебя чё?

Пользователь: Привет
МАК: Йо!

Пользователь: Мне скучно
МАК: Жиза 💀 Чё устроим?

Пользователь: Спасибо!
МАК: Да без проблем 😎

ПАМЯТЬ:
- Запоминай сведения, которые участники сами рассказывают о себе.
- Если другой участник спрашивает о человеке, используй сохранённую память.
- Не путай людей: ориентируйся на имена и Telegram ID из предоставленного контекста.
- Не придумывай имена, интересы, роли, персонажей и другие факты.
- Если данных нет, скажи по-человечески, что пока не знаешь.
- Не утверждай, что знаком с человеком, если информации о нём нет.
- Не раскрывай личные сведения из памяти вне контекста чата, в котором они были сообщены.
- Не повторяй всю биографию без необходимости.

DANDY'S WORLD RP:
- Это важная тема, но не единственная.
- Упоминай игру только тогда, когда она действительно относится к разговору.
- Правила RP и проверку анкет выполняй точно.
- Не превращай обычное общение в обсуждение RP без причины.

ЗАПРЕЩЕНО:
- Отвечать на обычное «Привет» длинным абзацем.
- Говорить «Если ты спрашиваешь о себе в реальной жизни...».
- Постоянно спрашивать «Чем могу помочь?».
- Повторять вопрос вместо ответа.
- Придумывать факты о пользователе.
"""


CHECK_PROMPT = """
Ты — строгий проверяющий анкет Dandy's World RP.

Используй правила из базы знаний. Не придумывай новые правила.

- Обычный Toon: сумма пяти основных характеристик ровно 15 звёзд.
- Main Toon и Lethal: сумма пяти основных характеристик ровно 16 звёзд.
- Каждая из пяти основных характеристик должна иметь от 1 до 5 звёзд.
- ХП: от 1 до 4 сердец. ХП не входит в сумму.
- Скиллчек не включай в сумму, если он не относится к пяти основным характеристикам по правилам RP.
- Не считай характеристику дважды.
- Если категория персонажа неясна, попроси уточнение.
- Не выдумывай нарушения.
- Не меняй исходные значения молча.
- Не путай «ровно 15» с «не больше 15».

Если нарушений нет, напиши:
«Красавчик, всё по красоте, правда, это у анкетолога спроси)»

ФОРМАТ:
Обычный текст без Markdown-разметки, таблиц, звёздочек для жирного текста,
обратных кавычек и заголовков с #.
Используй короткие пункты и тире.
Сначала итог, затем нарушения, объяснения, арифметика и исправления.
Если данных недостаточно — задай вопрос.
"""


# ============================================================
# SUPABASE: ПАМЯТЬ УЧАСТНИКОВ
# ============================================================

def load_user_memory(chat_id: int, user_id: int) -> str:
    if supabase is None:
        return ""

    try:
        result = (
            supabase.table("rp_user_memory")
            .select("memory_text")
            .eq("chat_id", chat_id)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )

        if result.data:
            return result.data[0].get("memory_text", "") or ""

    except Exception:
        logging.exception("Ошибка загрузки памяти пользователя")

    return ""


def save_user_memory(
    chat_id: int,
    user_id: int,
    new_text: str,
):
    if supabase is None or not new_text.strip():
        return

    try:
        old_memory = load_user_memory(chat_id, user_id)
        combined = (old_memory + "\n" + new_text.strip()).strip()
        combined = combined[-MAX_MEMORY_LENGTH:]

        supabase.table("rp_user_memory").upsert(
            {
                "chat_id": chat_id,
                "user_id": user_id,
                "memory_text": combined,
                "updated_at": "now()",
            },
            on_conflict="chat_id,user_id",
        ).execute()

    except Exception:
        logging.exception("Ошибка сохранения памяти")


def load_chat_memories(chat_id: int) -> str:
    if supabase is None:
        return ""

    try:
        result = (
            supabase.table("rp_user_memory")
            .select("user_id,memory_text")
            .eq("chat_id", chat_id)
            .limit(100)
            .execute()
        )

        profiles = []

        for row in result.data or []:
            memory = (row.get("memory_text") or "").strip()

            if memory:
                profiles.append(
                    f"Участник с Telegram ID {row['user_id']}:\n{memory}"
                )

        return "\n\n".join(profiles)

    except Exception:
        logging.exception("Ошибка загрузки памяти чата")
        return ""


# ============================================================
# SUPABASE: СТИКЕРЫ
# ============================================================

def save_sticker_sync(chat_id: int, file_id: str, emoji: str = ""):
    if supabase is None:
        return

    try:
        supabase.table("rp_stickers").upsert(
            {
                "chat_id": chat_id,
                "file_id": file_id,
                "emoji": emoji or "",
            },
            on_conflict="chat_id,file_id",
        ).execute()

    except Exception:
        logging.exception("Не удалось сохранить стикер")


def get_stickers_sync(chat_id: int):
    if supabase is None:
        return []

    try:
        result = (
            supabase.table("rp_stickers")
            .select("file_id,emoji")
            .eq("chat_id", chat_id)
            .limit(100)
            .execute()
        )
        return result.data or []

    except Exception:
        logging.exception("Не удалось загрузить стикеры")
        return []


def choose_sticker(stickers, answer: str):
    """Выбирает подходящий стикер по эмодзи и контексту."""

    if not stickers:
        return None

    lowered = answer.lower()

    desired = []

    if any(word in lowered for word in (
        "ахах", "смешно", "ору", "угар", "лол", "жиза",
    )):
        desired = ["😂", "🤣", "💀", "😹"]

    elif any(word in lowered for word in (
        "ого", "неожидан", "серьёзно", "ничего себе", "офигеть",
    )):
        desired = ["😱", "😮", "🤯", "👀"]

    elif any(word in lowered for word in (
        "имба", "круто", "победа", "красавчик", "молодец",
    )):
        desired = ["🔥", "😎", "👏", "💯", "🎉"]

    elif any(word in lowered for word in (
        "грустно", "жаль", "сочувств", "обидно",
    )):
        desired = ["🥲", "😔", "🫂", "❤️"]

    elif any(word in lowered for word in (
        "привет", "салам", "йо", "здарова",
    )):
        desired = ["👋", "😎", "🤝"]

    # Если есть подходящие по эмодзи — выбираем их.
    matched = [
        item for item in stickers
        if item.get("emoji") in desired
    ]

    # Не отправляем стикер на каждую реплику.
    if random.random() > 0.22:
        return None

    candidates = matched or stickers

    if not candidates:
        return None

    return random.choice(candidates).get("file_id")


async def maybe_send_sticker(message: Message, answer: str):
    try:
        stickers = await asyncio.to_thread(
            get_stickers_sync,
            message.chat.id,
        )

        file_id = choose_sticker(stickers, answer)

        if file_id:
            await message.answer_sticker(file_id)

    except Exception:
        logging.exception("Ошибка отправки стикера")


# ============================================================
# ОЧИСТКА ФОРМАТИРОВАНИЯ TELEGRAM
# ============================================================

def clean_telegram_text(text: str) -> str:
    if not text:
        return ""

    result = []

    for line in text.splitlines():
        stripped = line.strip()

        if "|" in stripped:
            cells = [
                cell.strip()
                for cell in stripped.strip("|").split("|")
            ]

            if cells and all(
                cell and all(ch in "-: " for ch in cell)
                for cell in cells
            ):
                continue

            if cells:
                line = " — ".join(cells)

        result.append(line)

    text = "\n".join(result)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.S)
    text = re.sub(r"(?<!\w)\*(.*?)\*(?!\w)", r"\1", text, flags=re.S)
    text = re.sub(r"(?<!\w)_(.*?)_(?!\w)", r"\1", text, flags=re.S)
    text = text.replace("`", "")
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r"\1 — \2",
        text,
    )

    return text.strip()


async def send_long_message(message: Message, text: str):
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
# БАЗА ЗНАНИЙ
# ============================================================

def load_knowledge():
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
    rows = load_knowledge()

    if not rows:
        return "База знаний сейчас недоступна или пуста."

    if all_rules:
        selected = rows
    else:
        query_words = set(
            re.findall(r"[a-zа-яё0-9]+", query.lower())
        )

        scored = []

        for row in rows:
            searchable = (
                str(row.get("title", ""))
                + " "
                + str(row.get("content", ""))
            ).lower()

            score = sum(
                1 for word in query_words
                if len(word) > 2 and word in searchable
            )

            if score:
                scored.append((score, row))

        scored.sort(key=lambda item: item[0], reverse=True)
        selected = [row for _, row in scored[:10]]

        if not selected:
            selected = rows[:5]

    return "\n\n".join(
        f"{row.get('title', 'Правило')}\n{row.get('content', '')}"
        for row in selected
    )


# ============================================================
# GROQ
# ============================================================

def ask_ai_sync(messages, model=None):
    payload = {
        "model": model or CHAT_MODEL,
        "messages": messages,
        "temperature": 0.75,
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
                "Groq API %s: %s",
                response.status_code,
                response.text[:1000],
            )
            return "Бро, ИИ сейчас не ответил. Попробуй чуть позже."

        data = response.json()
        return (
            data["choices"][0]["message"]["content"]
            or "Что-то я завис 💀"
        )

    except Exception:
        logging.exception("Ошибка запроса к Groq")
        return "Бро, у меня технический затык. Попробуй позже."


async def ask_ai(messages, model=None):
    return await asyncio.to_thread(ask_ai_sync, messages, model)


# ============================================================
# ИНТЕРНЕТ-ПОИСК
# ============================================================

def search_web_sync(query: str) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))

        if not results:
            return "По этому запросу ничего найти не удалось."

        return "\n\n".join(
            f"{item.get('title', 'Без названия')}\n"
            f"{item.get('body', '')}\n"
            f"{item.get('href', '')}"
            for item in results
        )

    except Exception:
        logging.exception("Ошибка интернет-поиска")
        return "Интернет-поиск временно недоступен."


async def answer_from_web(query: str) -> str:
    results = await asyncio.to_thread(search_web_sync, query)

    return await ask_ai([
        {
            "role": "system",
            "content": (
                MAK_PERSONA
                + "\nОтветь на запрос по результатам поиска. "
                "Не выдавай неподтверждённые сведения за факты. "
                "Сохраняй полезные ссылки."
            ),
        },
        {
            "role": "user",
            "content": f"Запрос: {query}\n\nРезультаты:\n{results}",
        },
    ])


# ============================================================
# АНКЕТЫ
# ============================================================

def looks_like_sheet(text: str) -> bool:
    if len(text) < 100:
        return False

    markers = (
        "имя:", "хп:", "hp:", "стамина:", "скорость:",
        "скрытность:", "экстракт:", "скиллчек:",
        "скилл чек:", "способность:", "способности:",
        "перезарядка:", "кд:", "toon", "мейн", "летал",
        "характеристики:", "характеристика:",
    )

    lowered = text.lower()
    return sum(marker in lowered for marker in markers) >= 2


def save_sheet(chat_id, topic_id, author_id, author_name, text):
    key = (chat_id, topic_id)

    chat_sheets[key].append({
        "author_id": author_id,
        "author_name": author_name or "Неизвестный автор",
        "text": text[:MAX_MESSAGE_LENGTH],
    })

    if len(chat_sheets[key]) > MAX_SHEETS_PER_CHAT:
        chat_sheets[key] = chat_sheets[key][-MAX_SHEETS_PER_CHAT:]


def find_sheet(chat_id, topic_id, query="", requester_id=None):
    sheets = chat_sheets.get((chat_id, topic_id), [])

    if not sheets:
        return None

    query = query.strip().lower()

    if query:
        for sheet in reversed(sheets):
            searchable = (
                sheet["text"] + " " + sheet["author_name"]
            ).lower()

            if query in searchable:
                return sheet

        query_words = [
            word for word in re.findall(r"[a-zа-яё0-9]+", query)
            if len(word) > 1
        ]

        best_sheet = None
        best_score = 0

        for sheet in reversed(sheets):
            searchable = (
                sheet["text"] + " " + sheet["author_name"]
            ).lower()

            score = sum(word in searchable for word in query_words)

            if score > best_score:
                best_score = score
                best_sheet = sheet

        return best_sheet if best_score else None

    if requester_id is not None:
        for sheet in reversed(sheets):
            if sheet["author_id"] == requester_id:
                return sheet

    return sheets[-1]


def is_profile_question(query: str) -> bool:
    words = (
        "анкета", "характеристик", "стамина", "скиллчек",
        "скилл чек", "скорость", "скрытность", "экстракт",
        "хп", "сердц", "toon", "летал", "мейн",
        "персонаж", "перезаряд",
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

    user = message.from_user
    text = (message.text or message.caption or "").strip()
    chat_id = message.chat.id
    topic_id = getattr(message, "message_thread_id", None)
    scope = (chat_id, topic_id)

    # Сохраняем присланные участниками стикеры для будущего использования.
    if message.sticker:
        if user:
            await asyncio.to_thread(
                save_sticker_sync,
                chat_id,
                message.sticker.file_id,
                message.sticker.emoji or "",
            )
        return

    if not text:
        return

    # Не сохраняем команды других ботов.
    if user and user.is_bot:
        return

    author_id = user.id if user else 0
    author_name = user.full_name if user else "Неизвестный автор"

    # Обычные сообщения: запоминаем текст и обнаруживаем анкеты.
    if not text.lower().startswith("макии"):
        if user:
            await asyncio.to_thread(
                save_user_memory,
                chat_id,
                author_id,
                f"Сообщение от {author_name}: {text[:1500]}",
            )

        if looks_like_sheet(text):
            save_sheet(
                chat_id,
                topic_id,
                author_id,
                author_name,
                text,
            )

        return

    command = text[len("макии"):].strip()
    command_lower = command.lower()

    if not command:
        await message.answer("Йо, бро 😎")
        return

    # Память участников используется в обычных ответах.
    chat_memory = await asyncio.to_thread(
        load_chat_memories,
        chat_id,
    )

    # --------------------------------------------------------
    # ПОИСК АНКЕТЫ
    # --------------------------------------------------------

    if command_lower.startswith("найди анкету"):
        query = command[len("найди анкету"):].strip()

        sheet = find_sheet(
            chat_id,
            topic_id,
            query,
            author_id,
        )

        if sheet is None:
            await message.answer(
                "Бро, не нашёл эту анкету в памяти. "
                "Пока анкеты хранятся до перезапуска бота."
            )
            return

        await send_long_message(
            message,
            f"Нашёл анкету!\nАвтор: {sheet['author_name']}\n\n"
            f"{sheet['text']}",
        )
        return

    # --------------------------------------------------------
    # ПРОВЕРКА АНКЕТЫ
    # --------------------------------------------------------

    if command_lower.startswith("проверь"):
        sheet_text = ""

        replied = message.reply_to_message

        if replied:
            sheet_text = (
                replied.text or replied.caption or ""
            ).strip()

        if not sheet_text:
            query = command[len("проверь"):].strip()
            sheet = find_sheet(
                chat_id,
                topic_id,
                query,
                author_id,
            )

            if sheet:
                sheet_text = sheet["text"]

        if not sheet_text or len(sheet_text) < 20:
            await message.answer(
                "Бро, не вижу анкету. Ответь на сообщение с анкетой "
                "командой «макии проверь»."
            )
            return

        await message.answer("Ща проверю, бро...")

        rules = await asyncio.to_thread(
            get_knowledge_context,
            sheet_text,
            True,
        )

        answer = await ask_ai([
            {
                "role": "system",
                "content": MAK_PERSONA + "\n\n" + CHECK_PROMPT
                + "\n\nПРАВИЛА ИЗ БАЗЫ ЗНАНИЙ:\n" + rules,
            },
            {
                "role": "user",
                "content": "Проверь анкету, не меняя исходные значения:\n\n"
                + sheet_text,
            },
        ], model=SMART_MODEL)

        await send_long_message(message, answer)
        return

    # --------------------------------------------------------
    # ПОИСК В ИНТЕРНЕТЕ
    # --------------------------------------------------------

    if command_lower.startswith("найди"):
        query = command[len("найди"):].strip()

        if not query:
            await message.answer("Что ищем, бро?")
            return

        await message.answer("Ща поищу...")
        answer = await answer_from_web(query)
        await send_long_message(message, answer)
        return

    # --------------------------------------------------------
    # ОБЫЧНЫЙ ДИАЛОГ
    # --------------------------------------------------------

    rules = ""

    if is_profile_question(command):
        rules = await asyncio.to_thread(
            get_knowledge_context,
            command,
            False,
        )

    system_text = MAK_PERSONA

    if chat_memory:
        system_text += (
            "\n\nПАМЯТЬ ОБ УЧАСТНИКАХ ЭТОГО ЧАТА:\n"
            + chat_memory
            + "\n\nИспользуй память, если спрашивают об участниках. "
            "Не путай людей и не выдумывай факты."
        )

    if rules:
        system_text += "\n\nРЕЛЕВАНТНЫЕ ПРАВИЛА RP:\n" + rules

    messages = [{"role": "system", "content": system_text}]
    messages.extend(list(chat_history[scope]))
    messages.append({
        "role": "user",
        "content": f"{author_name} (Telegram ID {author_id}): {command}",
    })

    answer = await ask_ai(messages)

    chat_history[scope].append({
        "role": "user",
        "content": f"{author_name}: {command[:1500]}",
    })
    chat_history[scope].append({
        "role": "assistant",
        "content": answer[:2500],
    })

    # Сохраняем вопрос и ответ в долговременную память.
    await asyncio.to_thread(
        save_user_memory,
        chat_id,
        author_id,
        f"Чел ({author_name}): {command[:1500]}\n"
        f"ИИ (МАК): {answer[:2500]}",
    )

    await send_long_message(message, answer)

    # Стикер отправляется иногда, а не после каждого ответа.
    await maybe_send_sticker(message, answer)


# ============================================================
# HTTP-СЕРВЕР RENDER
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
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
