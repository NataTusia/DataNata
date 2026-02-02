import os
import asyncio
import logging
import datetime
import time
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web
import aiohttp
import psycopg2 # Повернули стару бібліотеку
import google.generativeai as genai

# --- Налаштування ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
UNSPLASH_KEY = os.environ.get("UNSPLASH_ACCESS_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PORT = int(os.environ.get("PORT", 8080))

# Налаштування Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Підпис для помилок
ERROR_SIGNATURE = "\n\n📩 <b>Перешліть це повідомлення програмісту Наті, вона знає що з цим робити.</b>"

# --- Допоміжні функції (Взяті з твого старого коду) ---
def clean_text(text):
    text = text.replace("**", "").replace("### ", "").replace("## ", "")
    clean = re.compile('<.*?>')
    return re.sub(clean, '', text).strip()

def connect_to_db():
    # Проста функція підключення через psycopg2
    return psycopg2.connect(DATABASE_URL)

# --- 1. Логіка AI (Gemini) ---
async def generate_ai_post(topic, prompt_text):
    try:
        sys_prompt = (
            f"Ти — автор Telegram-каналу 'Data Nata'. Твоя аудиторія — новачки в IT. "
            f"Стиль: дружній, зрозумілий, без води. Використовуй емодзі. "
            f"Пиши українською мовою. "
            f"Тема: {topic}. "
            f"Контекст: {prompt_text}. "
            f"Максимальна довжина — 950 символів."
        )
        # Використовуємо async версію Gemini, вона не блокує бота
        response = await model.generate_content_async(sys_prompt)
        return clean_text(response.text)
    except Exception as e:
        return f"ERROR_AI: {str(e)}"

# --- 2. Пошук фото ---
async def get_random_photo(query):
    if not UNSPLASH_KEY:
        return "https://images.unsplash.com/photo-1542831371-29b0f74f9713?q=80&w=1000&auto=format&fit=crop"
    
    url = f"https://api.unsplash.com/photos/random?query={query}&orientation=landscape&client_id={UNSPLASH_KEY}&t={int(time.time())}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data['urls']['regular']
    except Exception as e:
        logging.error(f"Unsplash Error: {e}")
    
    return "https://images.unsplash.com/photo-1542831371-29b0f74f9713?q=80&w=1000&auto=format&fit=crop"

# --- 3. Основна функція (Draft) ---
async def prepare_draft(manual_date=None, from_command=False):
    target_date = manual_date if manual_date else datetime.datetime.now().date()
    
    conn = None
    try:
        # СИНХРОННЕ підключення (як було раніше)
        conn = connect_to_db()
        cursor = conn.cursor()
        
        # Виконуємо SQL
        cursor.execute("SELECT * FROM content_plan WHERE publish_date = %s AND status = 'pending'", (target_date,))
        # fetchrow у psycopg2 немає, є fetchone, який повертає кортеж (id, date, topic...)
        # Тому нам треба звертатись за індексами: 0-id, 1-date, 2-topic, 3-prompt... (залежить від структури)
        # Або зробимо простіше - DictCursor, але щоб не ускладнювати, візьмемо дані так:
        row = cursor.fetchone()
        
        if row:
            # Важливо: Треба знати порядок колонок у твоїй таблиці. 
            # При створенні ми писали: id, publish_date, topic, prompt, photo_query, final_text, status
            post_id = row[0]
            topic = row[2]
            prompt_db = row[3]
            photo_query = row[4]
            final_text = row[5]
            
            if from_command:
                await bot.send_message(ADMIN_ID, f"👩‍💻 Data Nata: Готую пост про **{topic}**...")

            # Генерація тексту
            if not final_text:
                final_text = await generate_ai_post(topic, prompt_db)
                # Оновлюємо базу
                cursor.execute("UPDATE content_plan SET final_text=%s WHERE id=%s", (final_text, post_id))
                conn.commit() # У psycopg2 треба робити коміт!
            
            # Фото
            photo_url = await get_random_photo(photo_query)
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Опублікувати", callback_data=f"publish_{post_id}")],
                [InlineKeyboardButton(text="🖼 Інше фото", callback_data=f"photo_{post_id}")],
                [InlineKeyboardButton(text="📝 Інший текст", callback_data=f"text_{post_id}")]
            ])
            
            await bot.send_photo(
                chat_id=ADMIN_ID, 
                photo=photo_url, 
                caption=final_text[:1024], 
                reply_markup=keyboard
            )
        else:
            if from_command:
                await bot.send_message(ADMIN_ID, f"⚠️ На {target_date} планів немає.")
            
        cursor.close()
        conn.close()
        
    except Exception as e:
        if conn: conn.close()
        await bot.send_message(ADMIN_ID, f"🆘 Помилка: {e}{ERROR_SIGNATURE}", parse_mode="HTML")

# --- Команди ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 Data Nata Bot Online (Classic DB)\n/check - Перевірити план")

@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft(from_command=True)

# --- Кнопки (Callbacks) ---
@dp.callback_query(F.data.startswith("photo_"))
async def regen_photo(callback: types.CallbackQuery):
    post_id = int(callback.data.split("_")[1])
    conn = None
    try:
        await callback.answer("🔄 Шукаю нове фото...")
        conn = connect_to_db()
        cursor = conn.cursor()
        cursor.execute("SELECT photo_query FROM content_plan WHERE id=%s", (post_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row:
            new_photo_url = await get_random_photo(row[0])
            media = InputMediaPhoto(media=new_photo_url, caption=callback.message.caption)
            await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
    except Exception as e:
        if conn: conn.close()
        await callback.message.answer(f"Помилка: {e}")

@dp.callback_query(F.data.startswith("text_"))
async def regen_text(callback: types.CallbackQuery):
    post_id = int(callback.data.split("_")[1])
    conn = None
    try:
        await callback.answer("📝 Переписую текст...")
        conn = connect_to_db()
        cursor = conn.cursor()
        cursor.execute("SELECT topic, prompt FROM content_plan WHERE id=%s", (post_id,))
        row = cursor.fetchone()
        
        if row:
            new_text = await generate_ai_post(row[0], row[1])
            cursor.execute("UPDATE content_plan SET final_text=%s WHERE id=%s", (new_text, post_id))
            conn.commit()
            cursor.close()
            conn.close()
            
            await callback.message.edit_caption(caption=new_text[:1024], reply_markup=callback.message.reply_markup)
    except Exception as e:
        if conn: conn.close()
        await callback.message.answer(f"Помилка: {e}")

@dp.callback_query(F.data.startswith("publish_"))
async def publish_to_channel(callback: types.CallbackQuery):
    post_id = int(callback.data.split("_")[1])
    conn = None
    try:
        await bot.send_photo(chat_id=CHANNEL_ID, photo=callback.message.photo[-1].file_id, caption=callback.message.caption)
        
        conn = connect_to_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE content_plan SET status='done' WHERE id=%s", (post_id,))
        conn.commit()
        cursor.close()
        conn.close()
        
        await callback.message.edit_caption(caption=f"✅ <b>ОПУБЛІКОВАНО</b>\n\n{callback.message.caption}", parse_mode="HTML")
    except Exception as e:
         await callback.answer(f"Помилка публікації: {e}", show_alert=True)

# --- Web Server (Для Render Uptime) ---
async def handle(request): 
    return web.Response(text="Data Nata Bot is Running!")

async def main():
    logging.basicConfig(level=logging.INFO)
    
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0)
    scheduler.start()
    
    try:
        await bot.send_message(ADMIN_ID, "✨ Data Nata System Online (Psycopg2) 👩‍💻", parse_mode="HTML")
    except:
        pass

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())