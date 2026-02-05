import os
import asyncio
import logging
import datetime
import time
import random
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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
UNSPLASH_KEY = os.environ.get("UNSPLASH_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
PORT = int(os.environ.get("PORT", 8080))

genai.configure(api_key=GEMINI_API_KEY)
# Налаштування логування, щоб бачити, які запити не спрацювали
logging.basicConfig(level=logging.INFO)
model = genai.GenerativeModel('gemini-flash-latest')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- СПИСОК ГАРАНТОВАНИХ IT-ЗАПИТІВ (ЗАПАСНИЙ АЕРОДРОМ) ---
IT_QUERIES = [
    "programming setup",
    "developer desk code",
    "laptop with code screen",
    "software engineer working",
    "coding dark mode",
    "web development html css",
    "data science python monitor",
    "macbook keyboard code",
    "minimalist coding desk",
    "hackathon developers"
]

# --- ФУНКЦІЇ ---

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

async def generate_quiz_data(topic, prompt_text):
    try:
        sys_prompt = f"Квіз: {topic}. {prompt_text}. Формат: Питання?|В1|В2|В3|0"
        response = await model.generate_content_async(sys_prompt)
        return response.text.strip()
    except Exception:
        return "Тестове питання (AI ліміт)?|Так|Ні|Можливо|0"

async def generate_ai_text(topic, prompt_text, platform, has_photo):
    try:
        if platform == 'inst':
            tags_instruction = "Додай хештеги."
            char_limit = 950
        else:
            tags_instruction = "Додай один тег."
            char_limit = 1500 if not has_photo else 950

        sys_prompt = (
            f"Тема: {topic}. {prompt_text}. "
            f"Ліміт {char_limit}. Без Markdown. {tags_instruction}"
        )
        
        response = await model.generate_content_async(sys_prompt)
        text = response.text.replace("**", "").replace("__", "").replace("```", "").strip()
        if len(text) > char_limit: text = text[:char_limit]
        return text

    except Exception as e:
        error_msg = str(e)
        if "429" in error_msg or "Resource has been exhausted" in error_msg:
            return (
                f"🤖 **AI відпочиває (Ліміт запитів).**\n\n"
                f"Але фото система працює! Спочатку шукаємо твій запит з бази, якщо не вийде - беремо IT-класику."
            )
        return f"AI Error: {error_msg}"

# --- НОВА ФУНКЦІЯ: ПРІОРИТЕТНИЙ ПОШУК ФОТО ---
async def get_prioritized_photo_url(db_query):
    """
    Спроба 1: Використати запит з бази даних (якщо він є).
    Спроба 2: Якщо Спроба 1 не вдалася або запиту не було, використати випадковий IT-запит.
    """
    if not UNSPLASH_KEY:
        return None, "❌ ПОМИЛКА: Немає ключа UNSPLASH_KEY"

    # 1. Формуємо чергу запитів
    queries_to_try = []
    
    # Якщо в базі щось написано, додаємо це першим у чергу
    if db_query and db_query.strip():
        queries_to_try.append(db_query.strip())
    
    # Додаємо запасний IT-варіант
    fallback = random.choice(IT_QUERIES)
    # Додаємо його, тільки якщо він відрізняється від того, що в базі (щоб не шукати двічі те саме)
    if not db_query or fallback.lower() != db_query.strip().lower():
        queries_to_try.append(fallback)

    last_error = "No queries tried"

    async with aiohttp.ClientSession() as session:
        # Проходимося по черзі (спочатку база, потім запасний)
        for query in queries_to_try:
            clean_query = urllib.parse.quote(query)
            api_url = f"[https://api.unsplash.com/photos/random?query=](https://api.unsplash.com/photos/random?query=){clean_query}&orientation=landscape&client_id={UNSPLASH_KEY}&t={int(time.time())}"
            
            try:
                logging.info(f"Trying Unsplash query: '{query}'...")
                async with session.get(api_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        raw_url = data['urls']['regular']
                        logging.info(f"Success with query: '{query}'")
                        # УСПІХ! Повертаємо URL і запит, який спрацював
                        return raw_url.strip(), query
                    else:
                        # Якщо не вийшло, записуємо помилку і йдемо до наступного запиту в черзі
                        error_text = await resp.text()
                        last_error = f"Query '{query}' failed: Status {resp.status}"
                        logging.warning(last_error)
            except Exception as e:
                 last_error = f"Connection error for '{query}': {str(e)}"
                 logging.error(last_error)
                 
    # Якщо ми пройшли всю чергу і нічого не знайшли
    return None, f"All attempts failed. Last error: {last_error}"

# --- ОСНОВНА ЛОГІКА ---

async def prepare_draft(platform, manual_day=None, from_command=False):
    table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
    day_now = manual_day if manual_day else datetime.datetime.now().day
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Читаємо photo_query з бази!
        if platform == 'tg':
            cursor.execute(f"SELECT topic, ai_prompt, photo_query, quiz_data FROM {table_name} WHERE day = %s", (day_now,))
        else:
            cursor.execute(f"SELECT topic, ai_prompt, photo_query FROM {table_name} WHERE day = %s", (day_now,))
            
        row = cursor.fetchone()
        
        if row:
            topic = row[0]
            ai_prompt = row[1]
            # Ось наш запит з бази (може бути None, якщо там пусто)
            db_photo_query = row[2] 
            quiz_data = row[3] if platform == 'tg' and len(row) > 3 else None
            
            is_quiz = (platform == 'tg' and db_photo_query is None and "квіз" in topic.lower())
            has_photo = (platform == 'inst') or (platform == 'tg' and not is_quiz)
            
            if from_command:
                # Показуємо адміну, що плануємо шукати
                Query_info = f"Бажання з бази: '{db_photo_query}'" if db_photo_query else "Бажання з бази: (пусто, буде авто-IT)"
                await bot.send_message(ADMIN_ID, f"👩‍💻 {platform}: {topic}\n🔎 {Query_info}")

            generated_text = await generate_ai_text(topic, ai_prompt, platform, has_photo)
            
            if is_quiz and not quiz_data:
                quiz_data = await generate_quiz_data(topic, ai_prompt)
                if quiz_data and "AI Error" not in quiz_data:
                    cursor.execute(f"UPDATE {table_name} SET quiz_data = %s WHERE day = %s", (quiz_data, day_now))
                    conn.commit()

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Опублікувати", callback_data=f"pub_{platform}_{day_now}")],
                [InlineKeyboardButton(text="📝 Переписати", callback_data=f"txt_{platform}_{day_now}")]
            ])

            if is_quiz and quiz_data and "AI Error" not in quiz_data:
                p = quiz_data.split("|")
                if len(p) >= 5:
                    await bot.send_message(ADMIN_ID, f"<b>🧠 Завдання:</b>\n{generated_text}", parse_mode="HTML", reply_markup=keyboard)
                    await bot.send_poll(chat_id=ADMIN_ID, question=p[0], options=p[1:4], type='quiz', correct_option_id=int(p[4]))
                else:
                    await bot.send_message(ADMIN_ID, f"Помилка квізу:\n{generated_text}", reply_markup=keyboard)

            elif has_photo:
                # Викликаємо нову розумну функцію, передаємо їй бажання з бази
                photo_url, used_query = await get_prioritized_photo_url(db_photo_query)
                
                if photo_url:
                    keyboard.inline_keyboard.append([InlineKeyboardButton(text="🖼 Інше фото", callback_data=f"pic_{platform}_{day_now}")])
                    # Показуємо, який запит врешті-решт спрацював
                    caption_with_info = f"{generated_text}\n\n(Знайдено за запитом: {used_query})"
                    await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=caption_with_info, reply_markup=keyboard)
                else:
                    # Якщо не вдалося знайти ні за базою, ні за запасним варіантом
                    error_report = f"⚠️ ПОМИЛКА ФОТО (Всі спроби невдалі).\nДеталі: {used_query}\n\nТекст посту:\n{generated_text}"
                    await bot.send_message(ADMIN_ID, error_report, reply_markup=keyboard)

            else: 
                await bot.send_message(ADMIN_ID, generated_text, reply_markup=keyboard)

        else:
            if from_command:
                await bot.send_message(ADMIN_ID, f"⚠️ Немає планів на день {day_now}.")
        
        cursor.close()
        conn.close()

    except Exception as e:
        if conn: conn.close()
        await bot.send_message(ADMIN_ID, f"🆘 Помилка бота: {str(e)}")

# --- ОБРОБНИКИ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 Bot Online (Hybrid Photo Logic)")

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
            # Чистимо від наших службових приписок про фото
            text_to_publish = text_to_publish.replace("🧠 Завдання:", "").strip()
            text_to_publish = text_to_publish.split("\n\n(Знайдено за запитом:", 1)[0].strip()
            if "⚠️ ПОМИЛКА ФОТО" in text_to_publish:
                 parts = text_to_publish.split("Текст посту:", 1)
                 if len(parts) > 1:
                     text_to_publish = parts[1].strip()

        if platform == 'tg':
            if callback.message.photo:
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
                 if len(p) >= 5:
                    await bot.send_poll(CHANNEL_ID, question=p[0], options=p[1:4], type='quiz', correct_option_id=int(p[4]))
                 
            msg = "✅ ОПУБЛІКОВАНО"
        else:
            msg = "✅ ЗАТВЕРДЖЕНО (Інста)"

        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(msg)

    except Exception as e:
        await callback.answer(f"❌ ПОМИЛКА: {str(e)}", show_alert=True)

# ЗМІНА ФОТО (Теж використовує гібридну логіку)
@dp.callback_query(F.data.startswith("pic_"))
async def cb_pic(callback: types.CallbackQuery):
    try:
        _, platform, day_str = callback.data.split("_")
        day_num = int(day_str)
        table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
        
        conn = get_db_connection()
        cursor = conn.cursor()
        # Читаємо запит з бази
        cursor.execute(f"SELECT photo_query FROM {table_name} WHERE day = %s", (day_num,))
        row = cursor.fetchone()
        conn.close()
        
        db_query = row[0] if row else None

        # Викликаємо розумну функцію
        new_url, used_query = await get_prioritized_photo_url(db_query)
        
        if new_url:
            # Оновлюємо підпис
            current_caption = callback.message.caption.split("\n\n(Знайдено за запитом:", 1)[0].strip()
            new_caption = f"{current_caption}\n\n(Знайдено за запитом: {used_query})"
            
            media = InputMediaPhoto(media=new_url, caption=new_caption)
            await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
        else:
            await callback.answer(f"Unsplash Error: {used_query}", show_alert=True)
                
    except Exception as e:
        await callback.answer(f"Err: {e}", show_alert=True)

# ТЕКСТ
@dp.callback_query(F.data.startswith("txt_"))
async def cb_txt(callback: types.CallbackQuery):
    _, platform, day_str = callback.data.split("_")
    day_num = int(day_str)
    table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT topic, ai_prompt FROM {table_name} WHERE day = %s", (day_num,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        topic, prompt = row[0], row[1]
        # Визначаємо, чи потрібне фото, базуючись на платформі та темі
        is_quiz = (platform == 'tg' and "квіз" in topic.lower())
        has_photo = (platform == 'inst') or (platform == 'tg' and not is_quiz)
        
        new_text = await generate_ai_text(topic, prompt, platform, has_photo)
        
        if callback.message.caption:
            # Зберігаємо інфо про фото
            photo_info = ""
            if "\n\n(Знайдено за запитом:" in callback.message.caption:
                 photo_info = "\n\n(Знайдено за запитом:" + callback.message.caption.split("\n\n(Знайдено за запитом:", 1)[1]
            
            await callback.message.edit_caption(caption=new_text + photo_info, reply_markup=callback.message.reply_markup)
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
    # logging.basicConfig вже викликано вище
    asyncio.run(main())