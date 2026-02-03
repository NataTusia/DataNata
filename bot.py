import os
import asyncio
import logging
import datetime
import time
import psycopg2
import google.generativeai as genai
import aiohttp
import urllib.parse
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАЛАШТУВАННЯ ---
# .strip() - це магія, яка видаляє випадкові пробіли, що вбивають бота
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
UNSPLASH_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
PORT = int(os.environ.get("PORT", 8080))

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- СТАБІЛЬНЕ ЗАПАСНЕ ФОТО ---
FALLBACK_PHOTO = "https://images.unsplash.com/photo-1542831371-29b0f74f9713?auto=format&fit=crop&w=1000&q=80"

# --- ФУНКЦІЇ ---

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

async def generate_quiz_data(topic, prompt_text):
    sys_prompt = (
        f"Створи квіз для Telegram. Тема: {topic}. Контекст: {prompt_text}. "
        f"Формат: Питання?|Відповідь1|Відповідь2|Відповідь3|НомерПравильної(0-2)"
    )
    try:
        response = await model.generate_content_async(sys_prompt)
        return response.text.strip()
    except:
        return None

async def generate_ai_text(topic, prompt_text, platform, has_photo):
    try:
        if has_photo:
            char_limit = 950   
            type_desc = "Змістовний, цікавий пост під фото"
        else:
            char_limit = 1500  
            type_desc = "Лаконічний пост."

        sys_prompt = (
            f"Ти — Data Nata. Пишеш для {platform}. "
            f"Тема: {topic}. Деталі: {prompt_text}. "
            f"Мова: Українська. "
            f"Вимоги: "
            f"1. {type_desc}. "
            f"2. Максимальний ліміт — {char_limit} символів. "
            f"3. В кінці додай ОДИН тег: #theory, #quiz, #lifehack або #start."
        )
        
        response = await model.generate_content_async(sys_prompt)
        text = response.text.replace("**", "").replace("__", "").replace("```", "").strip()
        
        if len(text) > char_limit:
            text = text[:char_limit]
            last_dot = text.rfind('.')
            if last_dot > 0:
                text = text[:last_dot+1]
            
        return text
    except Exception as e:
        return f"Помилка AI: {str(e)}"

# --- ПРОСТА ЛОГІКА ОТРИМАННЯ URL (ЯК В KIDSLAND) ---
async def get_photo_url(query):
    # Якщо ключа немає або запит пустий - віддаємо заглушку
    if not query or not UNSPLASH_KEY: 
        return FALLBACK_PHOTO
    
    # Кодуємо пробіли (dark mode -> dark%20mode)
    clean_query = urllib.parse.quote(query.strip())
    
    # Формуємо URL
    api_url = f"[https://api.unsplash.com/photos/random?query=](https://api.unsplash.com/photos/random?query=){clean_query}&orientation=landscape&client_id={UNSPLASH_KEY}&t={int(time.time())}"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw_url = data['urls']['regular']
                    # Важливо: чистимо результат від пробілів
                    return raw_url.strip()
                else:
                    logging.error(f"Unsplash API Error: {resp.status}")
    except Exception as e:
        logging.error(f"Unsplash Exception: {e}")

    # Якщо будь-яка помилка - повертаємо стабільне фото
    return FALLBACK_PHOTO

# --- ОСНОВНА ЛОГІКА ---

async def prepare_draft(platform, manual_day=None, from_command=False):
    table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
    day_now = manual_day if manual_day else datetime.datetime.now().day
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        if platform == 'tg':
            cursor.execute(f"SELECT topic, ai_prompt, photo_query, quiz_data FROM {table_name} WHERE day = %s", (day_now,))
        else:
            cursor.execute(f"SELECT topic, ai_prompt, photo_query FROM {table_name} WHERE day = %s", (day_now,))
            
        row = cursor.fetchone()
        
        if row:
            topic = row[0]
            ai_prompt = row[1]
            photo_query = row[2]
            quiz_data = row[3] if platform == 'tg' and row[3] else None
            
            is_quiz = (platform == 'tg' and photo_query is None and "квіз" in topic.lower())
            has_photo = photo_query is not None
            
            if from_command:
                await bot.send_message(ADMIN_ID, f"👩‍💻 {platform}: {topic}...")

            generated_text = await generate_ai_text(topic, ai_prompt, platform, has_photo)
            
            if is_quiz and not quiz_data:
                quiz_data = await generate_quiz_data(topic, ai_prompt)
                cursor.execute(f"UPDATE {table_name} SET quiz_data = %s WHERE day = %s", (quiz_data, day_now))
                conn.commit()

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Опублікувати", callback_data=f"pub_{platform}_{day_now}")],
                [InlineKeyboardButton(text="📝 Переписати", callback_data=f"txt_{platform}_{day_now}")]
            ])

            if is_quiz and quiz_data:
                p = quiz_data.split("|")
                await bot.send_message(ADMIN_ID, f"🧠 Завдання:\n{generated_text}")
                await bot.send_poll(chat_id=ADMIN_ID, question=p[0], options=p[1:4], type='quiz', correct_option_id=int(p[4]), reply_markup=keyboard)

            elif has_photo:
                # Отримуємо просте посилання (або Fallback)
                photo_url = await get_photo_url(photo_query)
                keyboard.inline_keyboard.append([InlineKeyboardButton(text="🖼 Інше фото", callback_data=f"pic_{platform}_{day_now}")])
                
                # Відправляємо URL
                await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=generated_text, reply_markup=keyboard)

            else: 
                await bot.send_message(ADMIN_ID, generated_text, reply_markup=keyboard)

        else:
            if from_command:
                await bot.send_message(ADMIN_ID, f"⚠️ Немає планів на день {day_now}.")
        
        cursor.close()
        conn.close()

    except Exception as e:
        if conn: conn.close()
        await bot.send_message(ADMIN_ID, f"🆘 Помилка: {e}")

# --- ОБРОБНИКИ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 Bot Online (Classic Mode)")

@dp.message(Command("generate_tg"))
async def cmd_gen_tg(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft('tg', from_command=True)

@dp.message(Command("generate_inst"))
async def cmd_gen_inst(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft('inst', from_command=True)

# ПУБЛІКАЦІЯ
@dp.callback_query(F.data.startswith("pub_"))
async def cb_publish(callback: types.CallbackQuery):
    try:
        _, platform, day_str = callback.data.split("_")
        day_num = int(day_str)
        
        text_to_publish = callback.message.caption if callback.message.caption else callback.message.text
        if text_to_publish:
            text_to_publish = text_to_publish.replace("🧠 Завдання:\n", "")
        
        if platform == 'tg':
            if callback.message.photo:
                # Використовуємо file_id, це надійно
                file_id = callback.message.photo[-1].file_id
                await bot.send_photo(CHANNEL_ID, photo=file_id, caption=text_to_publish[:1000])
            elif text_to_publish:
                await bot.send_message(CHANNEL_ID, text_to_publish[:4000])
            
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT quiz_data FROM telegram_plan WHERE day=%s", (day_num,))
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                 p = row[0].split("|")
                 await bot.send_poll(CHANNEL_ID, question=p[0], options=p[1:4], type='quiz', correct_option_id=int(p[4]))
                 
            msg = "✅ ОПУБЛІКОВАНО"
        else:
            msg = "✅ ЗАТВЕРДЖЕНО (Інста)"

        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(msg)

    except Exception as e:
        await callback.answer(f"❌ ПОМИЛКА: {str(e)}", show_alert=True)

# ЗМІНА ФОТО (URL method)
@dp.callback_query(F.data.startswith("pic_"))
async def cb_pic(callback: types.CallbackQuery):
    try:
        _, platform, day_str = callback.data.split("_")
        day_num = int(day_str)
        table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"SELECT photo_query FROM {table_name} WHERE day = %s", (day_num,))
        row = cursor.fetchone()
        conn.close()
        
        if row and row[0]:
            new_url = await get_photo_url(row[0])
            media = InputMediaPhoto(media=new_url, caption=callback.message.caption)
            await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.answer(f"Err: {e}", show_alert=True)

# ТЕКСТ (без змін)
@dp.callback_query(F.data.startswith("txt_"))
async def cb_txt(callback: types.CallbackQuery):
    _, platform, day_str = callback.data.split("_")
    day_num = int(day_str)
    table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT topic, ai_prompt, photo_query FROM {table_name} WHERE day = %s", (day_num,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        topic, prompt, photo_q = row
        has_photo = photo_q is not None
        new_text = await generate_ai_text(topic, prompt, platform, has_photo)
        
        if callback.message.caption:
            await callback.message.edit_caption(caption=new_text, reply_markup=callback.message.reply_markup)
        else:
            await callback.message.edit_text(text=new_text, reply_markup=callback.message.reply_markup)

# --- SERVER ---
async def handle(request): return web.Response(text="Bot is Alive")

async def main():
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
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())