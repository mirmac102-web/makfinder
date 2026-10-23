import asyncio
import logging
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from aiogram import Bot, Dispatcher, types

# --- МИНИ-СЕРВЕР ДЛЯ RENDER (чтобы веб-сервис не засыпал) ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"MAK Bot is active and running!")

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# Запускаем веб-сервер в фоновом потоке
threading.Thread(target=run_web_server, daemon=True).start()

# --- КОД БОТА ---
TELEGRAM_TOKEN = "8682588496:AAHrYDO95xAjPLdeAByUKPUEGlNUTwKMxOY"
OPENROUTER_API_KEY = "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"

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Бесплатные модели OpenRouter с авто-переключением
FREE_MODELS = [
    "meta-llama/llama-3-8b-instruct:free",
    "openrouter/free",
    "qwen/qwen-2.5-72b-instruct:free"
]

chat_history = []

# Универсальная функция запроса к ИИ с перебором моделей
async def ask_openrouter(system_prompt, user_prompt):
    for model in FREE_MODELS:
        try:
            res = requests.post(
                url="https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ]
                },
                timeout=15
            )
            if res.status_code == 200:
                data = res.json()
                return data['choices'][0]['message']['content']
        except Exception:
            continue
    return "⚠️ Брат, че-то серваки легли или лимиты кончились. Попробуй еще раз чуть позже!"

@dp.message()
async def handle_messages(message: types.Message):
    global chat_history
    
    text = message.text or ""
    text_lower = text.lower()

    # 1. Если сообщение обычное (не от МАКа и не команда) — сохраняем в историю чата
    if text and not text_lower.startswith("мак"):
        author = message.from_user.first_name or "Участник"
        chat_history.append(f"{author}: {text}")
        if len(chat_history) > 60:
            chat_history.pop(0)
        return

    # 2. Обработка команд, начинающихся с «Мак»
    if text_lower.startswith("мак"):
        
        # --- КОМАНДА: Мак найди ---
        if text_lower.startswith("мак найди"):
            query = text[9:].strip() or "персонажа"
            status_msg = await message.reply("🔍 МАК сканирует чат и генерирует анкету, бро...")
            
            system_prompt = (
                "Ты — ИИ-чат-бот по имени МАК (умный бро, весёлый, дружелюбный, общаешься неформально, "
                "используешь сленг вроде 'бро', 'жесть', 'имба', 'угар', '💀', '🔥', подкалываешь, но даёшь точные ответы). "
                "Твоя задача — проанализировать историю сообщений чата и составить анкету персонажа/участника "
                "на основе его активности и текста по строгому шаблону.\n\n"
                "ПРАВИЛА И ОГРАНИЧЕНИЯ ДЛЯ АНКЕТЫ:\n"
                "1. Кто вы: любое существо / тун / монстр.\n"
                "2. Возраст: любой.\n"
                "3. Рост: обязателен (адекватный, 'не как ебаное здание').\n"
                "4. Характер: любой на основе общения в чате.\n"
                "5. Каноничные друзья: указать, с кем из участников чата или персонажей ладит.\n"
                "6. Роль и Способности (выбери одну роль: Тун, Мейн или Летальный):\n"
                "   - ТУН: баланс, КД абилок 60–100 сек. Суммарный лимит характеристик = строго 15 звёзд!\n"
                "   - МЕЙН: сильные способки, КД абилок 80–200 сек. Суммарный лимит характеристик = строго 16 звёзд!\n"
                "   - ЛЕТАЛЬНЫЙ: очень сильные способки (но не имба уровня бога/полубога, не рушит пол-полки). Суммарный лимит = строго 16 звёзд!\n"
                "7. Статистика (распредели звёзды в рамках лимита ролей 15 или 16!):\n"
                "   - ❤️ ХП: от 1 до 4 сердец.\n"
                "   - 🎯 Скилл чек: от 1 до 5 ★\n"
                "   - ⚡ Скорость: от 1 до 5 ★\n"
                "   - 🏃 Выносливость: от 1 до 5 ★ (1★ = 100 стамины, каждая +★ даёт +25% стамины, 5★ = 200 стамины)\n"
                "   - 👤 Скрытность: от 1 до 5 ★\n"
                "   - 🛠️ Экстракт: от 1 до 5 ★\n"
                "8. Внешность: описать внешность туна.\n\n"
                "ОФОРМЛЕНИЕ АНКЕТЫ:\n"
                "🔥 **АНКЕТА ПЕРСОНАЖА ОТ МАКa** 🔥\n"
                "👤 **Имя / Кто вы:** ...\n"
                "🎂 **Возраст:** ...\n"
                "📏 **Рост:** ...\n"
                "🎭 **Характер:** ...\n"
                "🤝 **Друзья / С кем ладит:** ...\n"
                "🏷️ **Тип:** [Тун / Мейн / Летальный]\n"
                "⚡ **Способность / Пассивка:** ... (КД: ... сек)\n"
                "📊 **Характеристики (Всего звёзд: X/15 или X/16):**\n"
                "- ❤️ ХП: [X/4 сердец]\n"
                "- 🎯 Скилл чек: [X/5 ★]\n"
                "- ⚡ Скорость: [X/5 ★]\n"
                "- 🏃 Выносливость: [X/5 ★] (... стамины)\n"
                "- 👤 Скрытность: [X/5 ★]\n"
                "- 🛠️ Скорость экстракта: [X/5 ★]\n"
                "🎨 **Внешность туна:** ...\n"
                "💬 **Комментарий МАКa:** (пару слов в стиле бро)"
            )
            context_text = "\n".join(chat_history) if chat_history else "Чат пока пуст."
            user_prompt = f"Запрос на поиск/создание: {query}\n\nИстория чата:\n{context_text}"
            
            res_text = await ask_openrouter(system_prompt, user_prompt)
            await status_msg.edit_text(res_text)

        # --- КОМАНДА: Мак проверь ---
        elif text_lower.startswith("мак проверь"):
            query = text[11:].strip()
            
            # Если пользователь сделал реплай на другое сообщение с анкетой, цепляем его текст
            target_text = query
            if message.reply_to_message and message.reply_to_message.text:
                target_text += f"\n\nАнкета из сообщения:\n{message.reply_to_message.text}"

            if not target_text:
                await message.reply("Бро, напиши после «Мак проверь» саму анкету или сделай реплай (ответ) на сообщение с анкетой, которую надо пробить!")
                return

            status_msg = await message.reply("🧐 МАК проверяет баланс, звезды и косяки...")
            
            system_prompt = (
                "Ты — ИИ-чат-бот МАК (умный бро, эксперт по балансу персонажей и анкет). "
                "Твоя задача — проверить присланную анкету на ошибки и баланс:\n"
                "1. Проверь лимит звёзд: для Туна строго максимум 15 звёзд, для Мейна и Летального — строго максимум 16 звёзд. "
                "Суммируй звёзды (Скилл чек + Скорость + Выносливость + Скрытность + Экстракт). Если больше — разнеси за перебор!\n"
                "2. Проверь КД способностей: Тун — 60–100 сек, Мейн — 80–200 сек.\n"
                "3. Проверь ХП (максимум 4 сердца) и рост (не должен быть 'как ебаное здание').\n"
                "Общайся в стиле крутого бро: указывай на ошибки с юмором, сленгом и иронией, хвали за имбу или критикуй за дисбаланс."
            )
            user_prompt = f"Проверь эту анкету:\n{target_text}"
            
            res_text = await ask_openrouter(system_prompt, user_prompt)
            await status_msg.edit_text(res_text)

        # --- ОБЫЧНЫЙ РЕЖИМ ЧАТА (Мак ...) ---
        else:
            user_query = text[3:].strip()
            if not user_query:
                user_query = "Йо, бро, ты тут?"

            system_prompt = (
                "Ты — ИИ-чат-бот по имени МАК. Твой характер: дружелюбный, живой, умный ИИ, общаешься как близкий бро. "
                "Можешь угорать, использовать сленг ('бро', 'йо', 'жесть', 'имба', 'угар'), CAPS для эмоций ('БРООО', 'ЧЕГО 💀'), "
                "эмодзи, шутки и иронию. Давай точные и умные ответы, помогай с идеями, не будь занудным корпоративным ассистентом. "
                "Твой девиз: 'Сначала разберёмся, потом сделаем имбу'. 💀🔥"
            )
            
            res_text = await ask_openrouter(system_prompt, user_query)
            await message.reply(res_text)

async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
