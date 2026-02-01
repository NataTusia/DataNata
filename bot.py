import os
import asyncio
import logging
import datetime
import time
import requests
import psycopg2
import re
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InputMediaPhoto
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# --- Налаштування ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
UNSPLASH_KEY = os.environ.get("UNSPLASH_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Налаштування Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash') # Використовуємо актуальну швидку модель

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ПІДПИС ДЛЯ ПОМИЛОК ---
ERROR_SIGNATURE = "\n\n📩 <b>Перешлите это сообщение программисту Нате, она знает что с этим делать и поможет вам исправить ошибку.</b>"

# --- Допоміжні функції ---
def clean_text(text):
    text = text.replace("### ", "").replace("## ", "")
    # Прибираємо зайві зірочки, якщо Gemini перестарався, але залишаємо жирний шрифт для Markdown
    clean = re.compile('<.*?>')
    return re.sub(clean, '', text).strip()

def connect_to_db_with_retry():
    for i in range(3):
        try:
            return psycopg2.connect(DATABASE_URL)
        except Exception as e:
            time.sleep(5)
            if i == 2: raise e

# --- 1. Логіка AI (GEMINI) ---
async def generate_ai_post(topic, context, platform):
    if platform == "tg":
        role_desc = "Ти автор Telegram-каналу 'Data Nata'. Ти розробниця Python."
        requirements = "Стиль: корисний, дружній, структурований, для новачків. Використовуй markdown (жирний шрифт для заголовків). Пиши українською."
    else: 
        role_desc = "Ти IT-блогер в Instagram (Data Nata)."
        requirements = "Стиль: естетичний, емоційний, короткий, lifestyle. Додай тематичні хештеги в кінці. Пиши українською."

    prompt = (
        f"{role_desc}\n"
        f"Тема посту: {topic}.\n"
        f"Деталі/Вказівки: {context}.\n"
        f"Вимоги: {requirements}\n"
        f"ВАЖЛИВО: Не пиши вступних слів (типу 'Ось твій пост'). Одразу пиши текст публікації."
    )
    
    try:
        response = model.generate_content(prompt)
        return clean_text(response.text)
    except Exception as e:
        return f"ERROR_AI: {str(e)}"

# --- 2. Пошук фото (Unsplash) ---
async def get_random_photo(keywords):
    if not keywords: return None # Якщо в базі пусто, фото не шукаємо

    # Рівень 1: Шукаємо те, що в базі
    url = f"https://api.unsplash.com/photos/random?query={keywords}&client_id={UNSPLASH_KEY}&orientation=landscape&count=1&t={int(time.time())}"
    try:
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0]['urls']['regular']
            elif isinstance(data, dict) and 'urls' in data:
                return data['urls']['regular']
        
        # Рівень 2: Запасний варіант (IT естетика)
        elif response.status_code == 404:
            backup_url = f"https://api.unsplash.com/photos/random?query=coding+setup+neon&client_id={UNSPLASH_KEY}&orientation=landscape&count=1&t={int(time.time())}"
            backup_response = requests.get(backup_url, timeout=10)
            if backup_response.status_code == 200:
                data = backup_response.json()
                return data['urls']['regular'] if 'urls' in data else None

    except Exception as e:
        logging.error(f"Unsplash Error: {e}")
    
    # Рівень 3: Аварійна заглушка (просто красивий код)
    return "https://images.unsplash.com/photo-1542831371-29b0f74f9713?q=80&w=1000&auto=format&fit=crop"

# --- 3. Основна функція (Адаптовано під нові таблиці) ---
async def prepare_draft(platform, manual_day=None, from_command=False):
    day_now = manual_day if manual_day else datetime.datetime.now().day
    
    # Визначаємо таблицю
    if platform == "tg":
        table_name = "telegram_plan"
        platform_name = "Telegram"
    else:
        table_name = "instagram_plan"
        platform_name = "Instagram"
    
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        
        # Запит під нову структуру (day, topic, ai_prompt, photo_query)
        cursor.execute(f"SELECT topic, ai_prompt, photo_query FROM {table_name} WHERE day = %s", (day_now,))
        result = cursor.fetchone()
        
        if result:
            topic, ai_prompt, photo_query = result
            
            # Сповіщення про старт
            if from_command:
                await bot.send_message(ADMIN_ID, f"🔮 Генерую для {platform_name} (День {day_now})...")
            elif not manual_day:
                await bot.send_message(ADMIN_ID, f"⏰ Час посту для {platform_name}!")

            # Генерація контенту
            full_post_text = await generate_ai_post(topic, ai_prompt, platform)
            photo_url = await get_random_photo(photo_query)
            
            # Обрізаємо для підпису (ліміт Телеграму 1024 символи)
            caption = full_post_text
            if len(caption) > 1020: caption = caption[:1015] + "..."
            
            # Кнопки
            builder = InlineKeyboardBuilder()
            if platform == "tg":
                builder.row(types.InlineKeyboardButton(text="✅ Опублікувати", callback_data="confirm_publish"))
            
            builder.row(
                types.InlineKeyboardButton(text="🖼 Інше фото", callback_data=f"photo_{platform}_{day_now}"),
                types.InlineKeyboardButton(text="📝 Інший текст", callback_data=f"text_{platform}_{day_now}")
            )
            
            # Відправка (з фото або без)
            if photo_url:
                await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=caption, reply_markup=builder.as_markup(), parse_mode="Markdown")
                # Якщо текст задовгий, хвостик шлемо окремо
                if len(full_post_text) > 1020:
                     await bot.send_message(chat_id=ADMIN_ID, text=full_post_text[1020:], parse_mode="Markdown")
            else:
                # Тільки текст (якщо в базі NULL для фото)
                await bot.send_message(chat_id=ADMIN_ID, text=full_post_text, reply_markup=builder.as_markup(), parse_mode="Markdown")

        else:
            if from_command:
                await bot.send_message(ADMIN_ID, f"⚠️ У таблиці {table_name} немає теми на день {day_now}!")
            
        cursor.close()
        conn.close()
    except Exception as e:
        await bot.send_message(ADMIN_ID, f"🆘 Помилка ({platform}): {e}{ERROR_SIGNATURE}", parse_mode="HTML")

# --- Обробка команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 Бот Data Nata на зв'язку!\n/generate_tg - Тест Телеграм\n/generate_inst - Тест Інста")

@dp.message(Command("generate_tg"))
async def cmd_gen_tg(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft(platform="tg", from_command=True)

@dp.message(Command("generate_inst"))
async def cmd_gen_inst(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft(platform="inst", from_command=True)

# --- Callbacks ---
@dp.callback_query(F.data.startswith("photo_"))
async def regen_photo(callback: types.CallbackQuery):
    _, platform, day = callback.data.split("_")
    day = int(day)
    table_name = "telegram_plan" if platform == "tg" else "instagram_plan"

    await callback.answer("🔄 Шукаю нове фото...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        cursor.execute(f"SELECT photo_query FROM {table_name} WHERE day = %s", (day,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result and result[0]:
            new_photo_url = await get_random_photo(result[0])
            media = InputMediaPhoto(media=new_photo_url, caption=callback.message.caption)
            await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
        else:
            await callback.answer("У цьому пості фото не передбачено.", show_alert=True)
            
    except Exception as e:
        await callback.message.answer(f"Помилка: {e}")

@dp.callback_query(F.data.startswith("text_"))
async def regen_text(callback: types.CallbackQuery):
    _, platform, day = callback.data.split("_")
    day = int(day)
    table_name = "telegram_plan" if platform == "tg" else "instagram_plan"

    await callback.answer("📝 Переписую текст...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        cursor.execute(f"SELECT topic, ai_prompt FROM {table_name} WHERE day = %s", (day,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result:
            new_text = await generate_ai_post(result[0], result[1], platform)
            
            # Якщо повідомлення з фото - міняємо caption
            if callback.message.photo:
                new_caption = new_text
                if len(new_caption) > 1020: new_caption = new_caption[:1015] + "..."
                await callback.message.edit_caption(caption=new_caption, reply_markup=callback.message.reply_markup, parse_mode="Markdown")
            # Якщо повідомлення текстове - міняємо text
            else:
                await callback.message.edit_text(text=new_text, reply_markup=callback.message.reply_markup, parse_mode="Markdown")
            
    except Exception as e:
        await callback.message.answer(f"Помилка: {e}")

@dp.callback_query(F.data == "confirm_publish")
async def publish_to_channel(callback: types.CallbackQuery):
    # Отримуємо текст (з підпису фото або з самого тексту)
    content = callback.message.caption if callback.message.caption else callback.message.text
    
    try:
        if callback.message.photo:
            await bot.send_photo(chat_id=CHANNEL_ID, photo=callback.message.photo[-1].file_id, caption=content, parse_mode="Markdown")
        else:
            await bot.send_message(chat_id=CHANNEL_ID, text=content, parse_mode="Markdown")
            
        success_msg = f"✅ <b>ОПУБЛІКОВАНО В КАНАЛ</b>\n\n{content[:50]}..."
        
        if callback.message.photo:
            await callback.message.edit_caption(caption=success_msg, parse_mode="HTML")
        else:
            await callback.message.edit_text(text=success_msg, parse_mode="HTML")
            
    except Exception as e:
        await callback.message.answer(f"Не вдалося опублікувати: {e}", show_alert=True)

# --- Сервер ---
async def handle(request): return web.Response(text="Data Nata Bot is Running!")

async def main():
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
    
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    # ТГ о 09:00
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0, args=['tg'], misfire_grace_time=3600)
    # Інста о 09:10
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=10, args=['inst'], misfire_grace_time=3600)
    scheduler.start()
    
    print("🤖 Бот запущений!")
    try:
        await bot.send_message(ADMIN_ID, "✨ Бот Data Nata активний! 🐍")
    except:
        pass

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())