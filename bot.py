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

ERROR_SIGNATURE = "\n\n📩 Пиши Наті, бот втомився."

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
        context = "Telegram" if platform == 'tg' else "Instagram"
        sys_prompt = (
            f"Ти — Data Nata. Це пост для {context}. "
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
async def prepare_draft(platform, manual_day=None, from_command=False):
    # Беремо сьогоднішній день (число місяця), якщо не вказано інше
    day_now = manual_day if manual_day else datetime.datetime.now().day
    
    table_name = "telegram_plan" if platform == 'tg' else "instagram_plan"
    
    conn = None
    try:
        conn = connect_to_db()
        cursor = conn.cursor()
        
        # Запит з використанням твоїх колонок: day, topic, ai_prompt, photo_query
        query = f"SELECT id, topic, ai_prompt, photo_query, final_text FROM {table_name} WHERE day = %s AND status = 'pending'"
        cursor.execute(query, (day_now,))
        row = cursor.fetchone()
        
        if row:
            post_id = row[0]
            topic = row[1]
            prompt_db = row[2]  # Це колонка ai_prompt
            photo_query = row[3] # Це колонка photo_query
            final_text = row[4]
            
            if from_command:
                await bot.send_message(ADMIN_ID, f"👩‍💻 {platform.upper()}: Готую пост (День {day_now})...")

            # Генерація
            if not final_text:
                final_text = await generate_ai_post(topic, prompt_db, platform)
                # Оновлюємо базу
                cursor.execute(f"UPDATE {table_name} SET final_text=%s WHERE id=%s", (final_text, post_id))
                conn.commit()
            
            # Фото
            photo_url = await get_random_photo(photo_query)
            
            # Кнопки
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
                await bot.send_message(ADMIN_ID, f"⚠️ У таблиці {table_name} немає планів на день {day_now}.")
            
        cursor.close()
        conn.close()
        
    except Exception as e:
        if conn: conn.close()
        await bot.send_message(ADMIN_ID, f"🆘 Помилка: {e}{ERROR_SIGNATURE}")

# --- Команди ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 Bot Online\n/generate_tg\n/generate_inst")

@dp.message(Command("generate_tg"))
async def cmd_tg(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft(platform='tg', from_command=True)

@dp.message(Command("generate_inst"))
async def cmd_inst(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft(platform='inst', from_command=True)

# --- Обробка кнопок ---
@dp.callback_query(F.data.startswith("pic_"))
async def regen_photo(callback: types.CallbackQuery):
    _, platform, post_id = callback.data.split("_")
    table_name = "telegram_plan" if platform == 'tg' else "instagram_plan"
    
    try:
        await callback.answer("🔄")
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

@dp.callback_query(F.data.startswith("txt_"))
async def regen_text(callback: types.CallbackQuery):
    _, platform, post_id = callback.data.split("_")
    table_name = "telegram_plan" if platform == 'tg' else "instagram_plan"
    
    try:
        await callback.answer("📝")
        conn = connect_to_db()
        cursor = conn.cursor()
        cursor.execute(f"SELECT topic, ai_prompt FROM {table_name} WHERE id=%s", (post_id,))
        row = cursor.fetchone()
        
        if row:
            new_text = await generate_ai_post(row[0], row[1], platform)
            cursor.execute(f"UPDATE {table_name} SET final_text=%s WHERE id=%s", (new_text, post_id))
            conn.commit()
            cursor.close()
            conn.close()
            await callback.message.edit_caption(caption=new_text[:1024], reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Error: {e}")

@dp.callback_query(F.data.startswith("pub_"))
async def publish_post(callback: types.CallbackQuery):
    _, platform, post_id = callback.data.split("_")
    table_name = "telegram_plan" if platform == 'tg' else "instagram_plan"
    
    try:
        if platform == 'tg':
            await bot.send_photo(chat_id=CHANNEL_ID, photo=callback.message.photo[-1].file_id, caption=callback.message.caption)
            msg = "✅ ОПУБЛІКОВАНО"
        else:
            msg = "✅ ЗАТВЕРДЖЕНО (Інстаграм)"

        conn = connect_to_db()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE {table_name} SET status='done' WHERE id=%s", (post_id,))
        conn.commit()
        cursor.close()
        conn.close()
        
        await callback.message.edit_caption(caption=f"{msg}\n\n{callback.message.caption}")
    except Exception as e:
         await callback.answer(f"Error: {e}", show_alert=True)

# --- Server ---
async def handle(request): return web.Response(text="Bot Running!")

async def main():
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0, args=['tg'])
    scheduler.add_job(prepare_draft, 'cron', hour=10, minute=0, args=['inst'])
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())