import os
import asyncio
import logging
import datetime
import time
import re
import psycopg2
import google.generativeai as genai
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- КОНФІГУРАЦІЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
UNSPLASH_KEY = os.environ.get("UNSPLASH_ACCESS_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
PORT = int(os.environ.get("PORT", 8080))

# Ініціалізація AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

# Ініціалізація Бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ДОПОМІЖНІ ФУНКЦІЇ ---

def get_db_connection():
    """Синхронне підключення до бази через psycopg2"""
    return psycopg2.connect(DATABASE_URL)

def clean_text(text):
    """Очистка від Markdown та HTML"""
    text = text.replace("```html", "").replace("```", "")
    text = text.replace("**", "").replace("__", "")
    text = text.replace("<b>", "").replace("</b>", "")
    text = text.replace("<i>", "").replace("</i>", "")
    return text.strip()

async def generate_ai_text(topic, prompt_text, platform):
    """Генерація тексту через Gemini"""
    try:
        platform_name = "Instagram" if platform == 'inst' else "Telegram"
        sys_prompt = (
            f"Ти — Data Nata. Пишеш для {platform_name}. "
            f"Тема: {topic}. Деталі: {prompt_text}. "
            f"Мова: Українська. "
            f"ВАЖЛИВО: Пиши звичайним текстом. Ніякого жирного шрифту (*), ніякого HTML."
        )
        response = await model.generate_content_async(sys_prompt)
        return clean_text(response.text)
    except Exception as e:
        return f"AI Error: {str(e)}"

async def get_photo_url(query):
    """Пошук фото на Unsplash"""
    if not UNSPLASH_KEY:
        return "https://images.unsplash.com/photo-1542831371-29b0f74f9713?q=80&w=1000&auto=format&fit=crop"
    
    # Додаємо timestamp, щоб уникнути кешування однакових фото
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

# --- ОСНОВНА ЛОГІКА (DRAFT) ---

async def prepare_draft(platform, manual_day=None, from_command=False):
    # Визначаємо таблицю
    table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
    
    # Визначаємо день (сьогоднішнє число)
    day_now = manual_day if manual_day else datetime.datetime.now().day
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Шукаємо запис за номером дня (day)
        query = f"SELECT id, topic, ai_prompt, photo_query, final_text FROM {table_name} WHERE day = %s AND status = 'pending'"
        cursor.execute(query, (day_now,))
        row = cursor.fetchone()
        
        if row:
            post_id = row[0]
            topic = row[1]
            ai_prompt = row[2]
            photo_query = row[3]
            final_text = row[4]
            
            if from_command:
                await bot.send_message(ADMIN_ID, f"👩‍💻 {platform.upper()}: Готую тему '{topic}'...")

            # 1. Якщо тексту немає - генеруємо
            if not final_text:
                final_text = await generate_ai_text(topic, ai_prompt, platform)
                # Зберігаємо в базу
                update_sql = f"UPDATE {table_name} SET final_text = %s WHERE id = %s"
                cursor.execute(update_sql, (final_text, post_id))
                conn.commit()
            
            # 2. Шукаємо фото
            photo_url = await get_photo_url(photo_query)
            
            # 3. Кнопки (зберігаємо платформу в callback)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Опублікувати", callback_data=f"pub_{platform}_{post_id}")],
                [InlineKeyboardButton(text="🖼 Інше фото", callback_data=f"pic_{platform}_{post_id}")],
                [InlineKeyboardButton(text="📝 Переписати", callback_data=f"txt_{platform}_{post_id}")]
            ])
            
            # 4. Відправляємо адміну (БЕЗ parse_mode)
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
        await bot.send_message(ADMIN_ID, f"🆘 Помилка: {e}")

# --- ОБРОБНИКИ КОМАНД ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 Data Nata Bot Online.\n\n/generate_tg - Пост для ТГ\n/generate_inst - Пост для Інсти")

@dp.message(Command("generate_tg"))
async def cmd_gen_tg(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft('tg', from_command=True)

@dp.message(Command("generate_inst"))
async def cmd_gen_inst(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft('inst', from_command=True)

# --- ОБРОБНИКИ КНОПОК ---

@dp.callback_query(F.data.startswith("pic_"))
async def callback_new_photo(callback: types.CallbackQuery):
    # pic_inst_5 -> platform=inst, id=5
    _, platform, post_id = callback.data.split("_")
    table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
    
    try:
        await callback.answer("🔄")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"SELECT photo_query FROM {table_name} WHERE id = %s", (post_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if row:
            new_url = await get_photo_url(row[0])
            media = InputMediaPhoto(media=new_url, caption=callback.message.caption)
            await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
            
    except Exception as e:
        await callback.message.answer(f"Err: {e}")

@dp.callback_query(F.data.startswith("txt_"))
async def callback_new_text(callback: types.CallbackQuery):
    _, platform, post_id = callback.data.split("_")
    table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
    
    try:
        await callback.answer("📝")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"SELECT topic, ai_prompt FROM {table_name} WHERE id = %s", (post_id,))
        row = cursor.fetchone()
        
        if row:
            new_text = await generate_ai_text(row[0], row[1], platform)
            
            # Update DB
            cursor.execute(f"UPDATE {table_name} SET final_text = %s WHERE id = %s", (new_text, post_id))
            conn.commit()
            cursor.close()
            conn.close()
            
            await callback.message.edit_caption(caption=new_text[:1024], reply_markup=callback.message.reply_markup)
            
    except Exception as e:
        await callback.message.answer(f"Err: {e}")

@dp.callback_query(F.data.startswith("pub_"))
async def callback_publish(callback: types.CallbackQuery):
    _, platform, post_id = callback.data.split("_")
    table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
    
    try:
        if platform == 'tg':
            # Публікуємо в канал
            await bot.send_photo(chat_id=CHANNEL_ID, photo=callback.message.photo[-1].file_id, caption=callback.message.caption)
            msg = "✅ ОПУБЛІКОВАНО В ТГ"
        else:
            # Для Інсти просто міняємо статус (бо API закрите)
            msg = "✅ ЗАТВЕРДЖЕНО (Запости в Instagram)"
            
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE {table_name} SET status = 'done' WHERE id = %s", (post_id,))
        conn.commit()
        cursor.close()
        conn.close()
        
        await callback.message.edit_caption(caption=f"{msg}\n\n{callback.message.caption}")
        
    except Exception as e:
        await callback.answer(f"Err: {e}", show_alert=True)

# --- WEB SERVER (Для Render) ---
async def handle(request):
    return web.Response(text="Bot is Alive")

async def main():
    # 1. Запуск сервера
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    
    # 2. Запуск планувальника (Щоб сам нагадував)
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    # 9:00 - Телеграм, 10:00 - Інстаграм
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0, args=['tg'])
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=10, args=['inst'])
    scheduler.start()
    
    # 3. Запуск бота
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())