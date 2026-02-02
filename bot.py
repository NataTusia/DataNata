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
import asyncpg
import google.generativeai as genai

# --- Налаштування ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
UNSPLASH_KEY = os.environ.get("UNSPLASH_ACCESS_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") # Твій ключ Gemini
PORT = int(os.environ.get("PORT", 8080))

# Налаштування Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash') # Використовуємо швидку модель

# Ініціалізація
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Підпис для помилок
ERROR_SIGNATURE = "\n\n📩 <b>Перешліть це повідомлення програмісту Наті, вона знає що з цим робити.</b>"

# --- Допоміжні функції ---
def clean_text(text):
    # Прибираємо зайве форматування, якщо модель вирішить додати забагато зірочок
    text = text.replace("**", "").replace("### ", "").replace("## ", "")
    clean = re.compile('<.*?>')
    return re.sub(clean, '', text).strip()

async def get_db_connection():
    return await asyncpg.connect(DATABASE_URL)

# --- 1. Логіка AI (Gemini) ---
async def generate_ai_post(topic, prompt_text):
    try:
        # Промпт спеціально під Data Nata
        sys_prompt = (
            f"Ти — автор Telegram-каналу 'Data Nata'. Твоя аудиторія — новачки в IT. "
            f"Стиль: дружній, зрозумілий, без води. Використовуй емодзі. "
            f"Пиши українською мовою. "
            f"Тема: {topic}. "
            f"Контекст: {prompt_text}. "
            f"Максимальна довжина — 900 символів."
        )
        
        # Асинхронний виклик Gemini
        response = await model.generate_content_async(sys_prompt)
        return clean_text(response.text)
    except Exception as e:
        return f"ERROR_AI: {str(e)}"

# --- 2. Пошук фото ---
async def get_random_photo(query):
    if not UNSPLASH_KEY:
        return "https://images.unsplash.com/photo-1542831371-29b0f74f9713?q=80&w=1000&auto=format&fit=crop"
    
    # Додаємо трохи рандому, щоб кеш не віддавав одне й те саме
    url = f"https://api.unsplash.com/photos/random?query={query}&orientation=landscape&client_id={UNSPLASH_KEY}&t={int(time.time())}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data['urls']['regular']
    except Exception as e:
        logging.error(f"Unsplash Error: {e}")
    
    # Запасне фото (IT Setup)
    return "https://images.unsplash.com/photo-1542831371-29b0f74f9713?q=80&w=1000&auto=format&fit=crop"

# --- 3. Основна функція ---
async def prepare_draft(manual_date=None, from_command=False):
    target_date = manual_date if manual_date else datetime.datetime.now().date()
    
    try:
        conn = await get_db_connection()
        # Шукаємо пост на сьогодні
        row = await conn.fetchrow(
            "SELECT * FROM content_plan WHERE publish_date = $1 AND status = 'pending'", 
            target_date
        )
        
        if row:
            post_id = row['id']
            topic = row['topic']
            final_text = row['final_text']
            photo_query = row['photo_query']
            
            if from_command:
                await bot.send_message(ADMIN_ID, f"👩‍💻 Генерую пост про: {topic}...")

            # Генерація тексту (якщо немає)
            if not final_text:
                final_text = await generate_ai_post(topic, row['prompt'])
                # Зберігаємо чернетку
                await conn.execute("UPDATE content_plan SET final_text=$1 WHERE id=$2", final_text, post_id)
            
            # Фото
            photo_url = await get_random_photo(photo_query)
            
            # Кнопки
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Опублікувати", callback_data=f"publish_{post_id}")],
                [InlineKeyboardButton(text="🖼 Інше фото", callback_data=f"photo_{post_id}")],
                [InlineKeyboardButton(text="📝 Інший текст", callback_data=f"text_{post_id}")]
            ])
            
            # Відправка
            await bot.send_photo(
                chat_id=ADMIN_ID, 
                photo=photo_url, 
                caption=final_text[:1024], 
                reply_markup=keyboard
            )
        else:
            if from_command:
                await bot.send_message(ADMIN_ID, f"⚠️ У плані немає постів на {target_date}!")
            
        await conn.close()
    except Exception as e:
        await bot.send_message(ADMIN_ID, f"🆘 Помилка: {e}{ERROR_SIGNATURE}", parse_mode="HTML")

# --- Обробка команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 Data Nata Bot Ready (Gemini)\n/check_today - Перевірити план на сьогодні")

@dp.message(Command("check_today"))
async def cmd_check(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft(from_command=True)

# --- Callbacks ---
@dp.callback_query(F.data.startswith("photo_"))
async def regen_photo(callback: types.CallbackQuery):
    post_id = int(callback.data.split("_")[1])
    try:
        await callback.answer("🔄 Шукаю нове фото...")
        conn = await get_db_connection()
        row = await conn.fetchrow("SELECT photo_query FROM content_plan WHERE id=$1", post_id)
        await conn.close()

        if row:
            new_photo_url = await get_random_photo(row['photo_query'])
            media = InputMediaPhoto(media=new_photo_url, caption=callback.message.caption)
            await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Помилка: {e}")

@dp.callback_query(F.data.startswith("text_"))
async def regen_text(callback: types.CallbackQuery):
    post_id = int(callback.data.split("_")[1])
    try:
        await callback.answer("📝 Переписую текст (Gemini)...")
        conn = await get_db_connection()
        row = await conn.fetchrow("SELECT topic, prompt FROM content_plan WHERE id=$1", post_id)
        
        if row:
            new_text = await generate_ai_post(row['topic'], row['prompt'])
            # Оновлюємо в базі
            await conn.execute("UPDATE content_plan SET final_text=$1 WHERE id=$2", new_text, post_id)
            await conn.close()
            
            await callback.message.edit_caption(caption=new_text[:1024], reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Помилка: {e}")

@dp.callback_query(F.data.startswith("publish_"))
async def publish_to_channel(callback: types.CallbackQuery):
    post_id = int(callback.data.split("_")[1])
    try:
        await bot.send_photo(chat_id=CHANNEL_ID, photo=callback.message.photo[-1].file_id, caption=callback.message.caption)
        
        conn = await get_db_connection()
        await conn.execute("UPDATE content_plan SET status='done' WHERE id=$1", post_id)
        await conn.close()
        
        await callback.message.edit_caption(caption=f"✅ <b>ОПУБЛІКОВАНО</b>\n\n{callback.message.caption}", parse_mode="HTML")
    except Exception as e:
         await callback.answer(f"Помилка публікації: {e}", show_alert=True)

# --- Сервер (Точно як у твоєму прикладі) ---
async def handle(request): return web.Response(text="Data Nata Bot Running")

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # 1. Запуск веб-сервера (Для Render/Uptime)
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    
    # 2. Планувальник
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0)
    scheduler.start()
    
    try:
        await bot.send_message(ADMIN_ID, "✨ Data Nata System Online (Gemini Powered) 👩‍💻", parse_mode="HTML")
    except:
        pass

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())