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
import psycopg2
import google.generativeai as genai

# --- Налаштування ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
UNSPLASH_KEY = os.environ.get("UNSPLASH_ACCESS_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PORT = int(os.environ.get("PORT", 8080))

# Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Підпис
ERROR_SIGNATURE = "\n\n📩 Напиши Наті, бот трохи втомився."

# --- Допоміжні ---
def clean_text(text):
    text = text.replace("```html", "").replace("```", "")
    text = text.replace("**", "").replace("__", "")
    text = text.replace("<b>", "").replace("</b>", "")
    return text.strip()

def connect_to_db():
    return psycopg2.connect(DATABASE_URL)

# --- AI ---
async def generate_ai_post(topic, prompt_text, platform):
    try:
        if platform == 'tg':
            context = "Це пост для Telegram-каналу."
        else:
            context = "Це пост для Instagram (емоційний, з хештегами)."

        sys_prompt = (
            f"Ти — Data Nata. {context} "
            f"Тема: {topic}. Деталі: {prompt_text}. "
            f"Мова: Українська. "
            f"Пиши простим текстом без жирного шрифту та HTML."
        )
        response = await model.generate_content_async(sys_prompt)
        return clean_text(response.text)
    except Exception as e:
        return f"ERROR_AI: {str(e)}"

# --- Фото ---
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

# --- Основна логіка ---
async def prepare_draft(platform, manual_date=None, from_command=False):
    target_date = manual_date if manual_date else datetime.datetime.now().date()
    
    # Визначаємо таблицю
    table_name = "telegram_plan" if platform == 'tg' else "instagram_plan"
    
    conn = None
    try:
        conn = connect_to_db()
        cursor = conn.cursor()
        
        # SQL запит до відповідної таблиці
        query = f"SELECT * FROM {table_name} WHERE publish_date = %s AND status = 'pending'"
        cursor.execute(query, (target_date,))
        row = cursor.fetchone()
        
        if row:
            # Структура таблиць однакова: 
            # 0=id, 1=date, 2=topic, 3=prompt, 4=photo_query, 5=text, 6=status
            post_id = row[0]
            topic = row[2]
            prompt_db = row[3]
            photo_query = row[4]
            final_text = row[5]
            
            if from_command:
                await bot.send_message(ADMIN_ID, f"👩‍💻 {platform.upper()}: Готую пост '{topic}'...")

            # Генерація тексту
            if not final_text:
                final_text = await generate_ai_post(topic, prompt_db, platform)
                # Запис в базу
                update_query = f"UPDATE {table_name} SET final_text=%s WHERE id=%s"
                cursor.execute(update_query, (final_text, post_id))
                conn.commit()
            
            # Фото
            photo_url = await get_random_photo(photo_query)
            
            # КНОПКИ: додаємо платформу в callback_data (tg_id або inst_id)
            # Формат: дія_платформа_id
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Опублікувати", callback_data=f"pub_{platform}_{post_id}")],
                [InlineKeyboardButton(text="🖼 Інше фото", callback_data=f"pic_{platform}_{post_id}")],
                [InlineKeyboardButton(text="📝 Переписати", callback_data=f"txt_{platform}_{post_id}")]
            ])
            
            await bot.send_photo(
                chat_id=ADMIN_ID, 
                photo=photo_url, 
                caption=final_text[:1024], 
                reply_markup=keyboard
            )
        else:
            if from_command:
                await bot.send_message(ADMIN_ID, f"⚠️ У таблиці {table_name} немає планів на {target_date}.")
            
        cursor.close()
        conn.close()
        
    except Exception as e:
        if conn: conn.close()
        await bot.send_message(ADMIN_ID, f"🆘 Помилка: {e}{ERROR_SIGNATURE}")

# --- Команди ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 Data Nata Bot\n/generate_tg\n/generate_inst")

@dp.message(Command("generate_tg"))
async def cmd_tg(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft(platform='tg', from_command=True)

@dp.message(Command("generate_inst"))
async def cmd_inst(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft(platform='inst', from_command=True)

# --- Обробка кнопок ---

# 1. ЗМІНА ФОТО
@dp.callback_query(F.data.startswith("pic_"))
async def regen_photo(callback: types.CallbackQuery):
    # Розбираємо: pic_tg_5 -> дія=pic, platform=tg, id=5
    _, platform, post_id = callback.data.split("_")
    table_name = "telegram_plan" if platform == 'tg' else "instagram_plan"
    
    conn = None
    try:
        await callback.answer("🔄 Шукаю нове фото...")
        conn = connect_to_db()
        cursor = conn.cursor()
        cursor.execute(f"SELECT photo_query FROM {table_name} WHERE id=%s", (post_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row:
            new_photo_url = await get_random_photo(row[0])
            media = InputMediaPhoto(media=new_photo_url, caption=callback.message.caption)
            await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Error: {e}")

# 2. ЗМІНА ТЕКСТУ
@dp.callback_query(F.data.startswith("txt_"))
async def regen_text(callback: types.CallbackQuery):
    _, platform, post_id = callback.data.split("_")
    table_name = "telegram_plan" if platform == 'tg' else "instagram_plan"
    
    conn = None
    try:
        await callback.answer("📝 Переписую...")
        conn = connect_to_db()
        cursor = conn.cursor()
        cursor.execute(f"SELECT topic, prompt FROM {table_name} WHERE id=%s", (post_id,))
        row = cursor.fetchone()
        
        if row:
            new_text = await generate_ai_post(row[0], row[1], platform)
            
            # Оновлюємо базу
            cursor.execute(f"UPDATE {table_name} SET final_text=%s WHERE id=%s", (new_text, post_id))
            conn.commit()
            cursor.close()
            conn.close()
            
            await callback.message.edit_caption(caption=new_text[:1024], reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Error: {e}")

# 3. ПУБЛІКАЦІЯ
@dp.callback_query(F.data.startswith("pub_"))
async def publish_post(callback: types.CallbackQuery):
    _, platform, post_id = callback.data.split("_")
    table_name = "telegram_plan" if platform == 'tg' else "instagram_plan"
    
    conn = None
    try:
        # Публікуємо (поки що все в один канал, або можна додати умову для Інсти)
        # Якщо це Інстаграм - бот просто напише, що пост готовий, бо в Інсту він сам не запостить (API закрите)
        if platform == 'tg':
            await bot.send_photo(chat_id=CHANNEL_ID, photo=callback.message.photo[-1].file_id, caption=callback.message.caption)
            status_msg = "✅ ОПУБЛІКОВАНО В ТЕЛЕГРАМ"
        else:
            status_msg = "✅ ЗАТВЕРДЖЕНО (Запости в Інстаграм вручну)"

        conn = connect_to_db()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE {table_name} SET status='done' WHERE id=%s", (post_id,))
        conn.commit()
        cursor.close()
        conn.close()
        
        await callback.message.edit_caption(caption=f"{status_msg}\n\n{callback.message.caption}")
    except Exception as e:
         await callback.answer(f"Error: {e}", show_alert=True)

# --- Сервер ---
async def handle(request): return web.Response(text="Bot is Running!")

async def main():
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0, args=['tg'])
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=10, args=['inst'])
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())