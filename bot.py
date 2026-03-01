import os
import asyncio
import logging
import datetime
import time
import psycopg2
import google.generativeai as genai
import aiohttp
import urllib.parse
import re
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, BufferedInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv  
load_dotenv()                   

# --- НОВІ ІМПОРТИ ДЛЯ КАРТИНОК ---
from PIL import Image, ImageDraw, ImageFont
import textwrap
from io import BytesIO

# --- НАЛАШТУВАННЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
UNSPLASH_KEY = os.environ.get("UNSPLASH_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
PORT = int(os.environ.get("PORT", 8080))

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ==========================================
# --- НОВИЙ БЛОК: ФАБРИКА КАРУСЕЛЕЙ 🏭 ---
# ==========================================

def create_slide(template_path, header_text, body_text, font_path, header_font_size, body_font_size, text_color, header_xy, body_xy, header_max_chars, body_max_chars):
    """Малює заголовок і текст, ЗБЕРІГАЮЧИ абзаци"""
    try:
        img = Image.open(template_path).convert("RGBA")
        draw = ImageDraw.Draw(img)
        
        try:
            font_header = ImageFont.truetype(font_path, header_font_size)
            font_body = ImageFont.truetype(font_path, body_font_size)
        except IOError:
            logging.error(f"Шрифт {font_path} не знайдено!")
            font_header = ImageFont.load_default()
            font_body = font_header

        # Супер-функція, яка зберігає абзаци (Enter) при перенесенні тексту
        def wrap_preserve_newlines(text, width):
            paragraphs = text.split('\n')
            wrapped_lines = []
            for p in paragraphs:
                if p.strip() == "":
                    wrapped_lines.append("") # Зберігаємо пусті рядки між абзацами
                else:
                    wrapped_lines.append(textwrap.fill(p, width=width))
            return '\n'.join(wrapped_lines)

        if header_text:
            wrapped_header = wrap_preserve_newlines(header_text, header_max_chars)
            draw.text(header_xy, wrapped_header, font=font_header, fill=text_color)

        if body_text:
            wrapped_body = wrap_preserve_newlines(body_text, body_max_chars)
            # Додаємо spacing=10, щоб між рядками було трохи повітря
            draw.text(body_xy, wrapped_body, font=font_body, fill=text_color, spacing=10)

        bio = BytesIO()
        img.save(bio, format='PNG')
        bio.seek(0)
        return bio
    except Exception as e:
        logging.error(f"Помилка малювання слайду: {e}")
        return None

async def generate_carousel_texts(topic, prompt_text):
    """Генерує слайди і лонгрід паралельно (із захистом від лімітів)"""
    sys_prompt_slides = (
        f"Ти контент-мейкер. Напиши текст для Instagram-каруселі (від 5 до 7 слайдів). "
        f"Тема: {topic}. Деталі з контент-плану: {prompt_text}. Мова: Українська. "
        "Вимоги до формату (СТРОГО):\n"
        "Слайд 1: [Великий Заголовок] - [Хук: 2-3 речення, що інтригують або дають коротке визначення]\n"
        "Слайд 2: [Заголовок] - [Розгорнутий текст: 5-8 речень. Обов'язково використовуй абзаци та маркери (•) для переліків]\n"
        "Слайд X: [Заголовок] - [Розгорнутий текст з абзацами...]\n"
        "Важливо: Розділяй абзаци і списки за допомогою переносу рядка (Enter). Жодних зірочок Markdown (**)."
    )
    
    sys_prompt_caption = (
        f"Ти IT-експерт Data Nata. Напиши глибокий, цікавий та структурований опис (caption) для Instagram-посту. "
        f"Тема: {topic}. Деталі: {prompt_text}. Мова: Українська. "
        "Вимоги до тексту:\n"
        "1. Обсяг: приблизно 2500-3000 символів. Має бути розгорнуто, як міні-стаття або урок.\n"
        "2. Структура: Хук (Гачок), Основна частина, Практика, Висновок та CTA.\n"
        "3. Хук: перші 2 рядки мають інтригувати.\n"
        "4. Основна частина: поясни тему дуже простою мовою, на життєвих прикладах.\n"
        "5. Практика: додай невеличкий приклад.\n"
        "6. Абзаци мають бути короткими. Використовуй емодзі для списків, але не перевантажуй.\n"
        "7. Ніякого Markdown (**).\n"
        "8. В кінці додай 10-15 релевантних хештегів (#python #datanata тощо).\n"
        "9. Стиль: дружній, підтримуючий, експертний."
    )
    
    try:
        task1 = model.generate_content_async(sys_prompt_slides)
        task2 = model.generate_content_async(sys_prompt_caption)
        res1, res2 = await asyncio.gather(task1, task2)
        
        slides_text = res1.text.replace("**", "").replace("__", "").replace("```", "").strip()
        caption_text = res2.text.replace("**", "").replace("__", "").replace("```", "").strip()
        
        # Захист Телеграму від завеликих повідомлень
        char_limit = 3500 
        if len(caption_text) > char_limit:
            caption_text = caption_text[:char_limit]
            last_dot = caption_text.rfind('.')
            if last_dot > 0:
                caption_text = caption_text[:last_dot+1]
                
        return slides_text, caption_text
    except Exception as e:
        logging.error(f"Помилка ШІ: {e}")
        return f"❌ Помилка генерації слайдів: {str(e)}", f"❌ Помилка генерації опису: {str(e)}"

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
        if platform == 'inst':
            tags_instruction = "В кінці додай хештеги (#python #coding...)."
            char_limit = 950
            type_desc = "Цікавий пост для Instagram."
        else:
            tags_instruction = "В кінці додай ОДИН тег: #theory, #quiz або #lifehack."
            if has_photo:
                char_limit = 950
                type_desc = "Корисний пост під фото."
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
            f"3. {tags_instruction} "
            f"4. НІЯКОГО Markdown. Не використовуй зірочки ** або нижні підкреслення __. Пиши просто текст."
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

# --- ОТРИМАННЯ ФОТО (ЗАЛИШЕНО БЕЗ ЗМІН ДЛЯ ТЕЛЕГРАМУ) ---
async def get_photo_url_debug(query):
    if not UNSPLASH_KEY:
        return None, "❌ ПОМИЛКА: Змінна UNSPLASH_KEY пуста в налаштуваннях Render!"

    if not query: query = "technology"
    clean_query = urllib.parse.quote(query.strip())
    
    api_url = f"[https://api.unsplash.com/photos/random?query=](https://api.unsplash.com/photos/random?query=){clean_query}&orientation=landscape&client_id={UNSPLASH_KEY}&t={int(time.time())}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw_url = data['urls']['regular']
                    return raw_url.strip(), None
                else:
                    error_text = await resp.text()
                    return None, f"Status {resp.status}: {error_text}"
    except Exception as e:
        return None, f"Connection Error: {str(e)}"

# --- ОСНОВНА ЛОГІКА ---

async def prepare_draft(platform, post_time='morning', manual_date=None, from_command=False):
    table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
    date_now = manual_date if manual_date else datetime.datetime.now().strftime('%Y-%m-%d')
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Шукаємо пости за датою і часом
        if platform == 'tg':
            cursor.execute(f"SELECT topic, ai_prompt, photo_query, quiz_data FROM {table_name} WHERE post_date = %s AND post_time = %s", (date_now, post_time))
        else:
            cursor.execute(f"SELECT topic, ai_prompt, photo_query FROM {table_name} WHERE post_date = %s", (date_now,))
            
        row = cursor.fetchone()
        
        if row:
            topic = row[0]
            ai_prompt = row[1]
            photo_query = row[2]
            quiz_data = row[3] if platform == 'tg' and len(row) > 3 and row[3] else None
            
            is_quiz = (platform == 'tg' and photo_query is None and "квіз" in topic.lower())
            has_photo = photo_query is not None
            
            if from_command:
                key_status = "✅ Ключ є" if UNSPLASH_KEY else "❌ Ключа немає"
                await bot.send_message(ADMIN_ID, f"👩‍💻 {platform} ({date_now} {post_time}): {topic} ({key_status})...")

            # ==========================================
            # НОВА ЛОГІКА ДЛЯ INSTAGRAM (КАРУСЕЛІ)
            # ==========================================
            if platform == 'inst':
                slides_text, caption_text = await generate_carousel_texts(topic, ai_prompt)
                
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🎨 Намалювати карусель", callback_data=f"draw_inst_{date_now}_{post_time}")],
                    [InlineKeyboardButton(text="📝 Переписати текст", callback_data=f"txt_{platform}_{date_now}_{post_time}")]
                ])
                
                await bot.send_message(ADMIN_ID, f"🖼 ТЕКСТ ДЛЯ СЛАЙДІВ ({date_now}):\n\n{slides_text}", reply_markup=keyboard)
                await bot.send_message(ADMIN_ID, f"📝 ОПИС ПІД ПОСТОМ:\n\n{caption_text}")

            # ==========================================
            # СТАРА ЛОГІКА ДЛЯ TELEGRAM
            # ==========================================
            else:
                generated_text = await generate_ai_text(topic, ai_prompt, platform, has_photo)
                
                if is_quiz and not quiz_data:
                    quiz_data = await generate_quiz_data(topic, ai_prompt)
                    # Оновлюємо базу з урахуванням дати і часу
                    cursor.execute(f"UPDATE {table_name} SET quiz_data = %s WHERE post_date = %s AND post_time = %s", (quiz_data, date_now, post_time))
                    conn.commit()

                # В усіх кнопках додаємо date_now та post_time
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Опублікувати", callback_data=f"pub_{platform}_{date_now}_{post_time}")],
                    [InlineKeyboardButton(text="📝 Переписати", callback_data=f"txt_{platform}_{date_now}_{post_time}")]
                ])

                if is_quiz and quiz_data:
                    p = quiz_data.split("|")
                    await bot.send_message(ADMIN_ID, f"<b>🧠 Завдання:</b>\n{generated_text}", parse_mode="HTML", reply_markup=keyboard)
                    await bot.send_poll(chat_id=ADMIN_ID, question=p[0], options=p[1:4], type='quiz', correct_option_id=int(p[4]))

                elif has_photo:
                    photo_url, error_msg = await get_photo_url_debug(photo_query)
                    
                    if photo_url:
                        keyboard.inline_keyboard.append([InlineKeyboardButton(text="🖼 Інше фото", callback_data=f"pic_{platform}_{date_now}_{post_time}")])
                        await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=generated_text, reply_markup=keyboard)
                    else:
                        error_report = f"⚠️ Unsplash Error: {error_msg}\n\n{generated_text}"
                        await bot.send_message(ADMIN_ID, error_report, reply_markup=keyboard)

                else: 
                    await bot.send_message(ADMIN_ID, generated_text, reply_markup=keyboard)

        else:
            if from_command:
                await bot.send_message(ADMIN_ID, f"⚠️ Немає планів на {date_now} ({post_time}).")
        
        cursor.close()
        conn.close()

    except Exception as e:
        if conn: conn.close()
        await bot.send_message(ADMIN_ID, f"🆘 Помилка: {str(e)}")

# --- ОБРОБНИКИ КОМАНД ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 Bot Online (Final Fix)")

@dp.message(Command("generate_tg_morning"))
async def cmd_gen_tg_morning(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft('tg', post_time='morning', from_command=True)

@dp.message(Command("generate_tg_evening"))
async def cmd_gen_tg_evening(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft('tg', post_time='evening', from_command=True)

@dp.message(Command("generate_inst"))
async def cmd_gen_inst(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft('inst', post_time='morning', from_command=True)

# ОБРОБНИК КНОПКИ "НАМАЛЮВАТИ КАРУСЕЛЬ"
@dp.callback_query(F.data.startswith("draw_inst_"))
async def cb_draw_carousel(callback: types.CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None) 
    msg = await callback.message.reply("🎨 Беру пензлі... Малюю карусель!")
    
    text = callback.message.text
    slides = re.split(r'Слайд \d+:', text)[1:] 
    
    if not slides:
        await msg.edit_text("❌ Не вдалося розпізнати структуру слайдів. Перевір, чи є слова 'Слайд 1:'")
        return
        
    media = []
    font_path = "font.ttf" 
    
    for i, slide_raw_text in enumerate(slides):
        slide_raw_text = slide_raw_text.strip()
        if not slide_raw_text: continue
        
        # Розумне розбиття на заголовок і текст
        if " - " in slide_raw_text:
            parts = slide_raw_text.split(" - ", 1)
            header = parts[0].strip()
            body = parts[1].strip()
        elif "\n" in slide_raw_text:
            parts = slide_raw_text.split("\n", 1)
            header = parts[0].strip()
            body = parts[1].strip()
        else:
            header = slide_raw_text
            body = ""
        
        is_cover = (i == 0)
        
        if is_cover:
            template = "cover_template.png"
            h_size, b_size = 80, 40 # Заголовок 80, текст 40
            h_xy, b_xy = (550, 260), (240, 700) # Координати для обкладинки
            h_chars, b_chars = 15, 35 # Скільки літер влазить в рядок
        else:
            template = "body_template.png"
            h_size, b_size = 80, 35 # Заголовок ТАКОЖ 80! Текст трохи менший (35), бо його багато
            h_xy, b_xy = (220, 220), (200, 490) # Координати: Заголовок вище, текст під ним
            h_chars, b_chars = 20, 57 

        if not os.path.exists(template):
            await msg.edit_text(f"❌ Не знайдено файл шаблону: {template}")
            return
            
        bio = create_slide(
            template_path=template,
            header_text=header,
            body_text=body,
            font_path=font_path,
            header_font_size=h_size,
            body_font_size=b_size,
            text_color=(255, 255, 255),
            header_xy=h_xy,
            body_xy=b_xy,
            header_max_chars=h_chars,
            body_max_chars=b_chars
        )
        
        if bio:
            media.append(InputMediaPhoto(media=BufferedInputFile(bio.read(), filename=f"slide_{i}.png")))
            
    if media:
        await bot.send_media_group(callback.message.chat.id, media=media)
        await msg.delete()
    else:
        await msg.edit_text("❌ Не вдалося згенерувати жодної картинки.")

# ПУБЛІКАЦІЯ
@dp.callback_query(F.data.startswith("pub_"))
async def cb_publish(callback: types.CallbackQuery):
    try:
        parts = callback.data.split("_")
        platform = parts[1]
        date_str = parts[2]
        post_time = parts[3] if len(parts) > 3 else 'morning'
        
        text_to_publish = callback.message.caption if callback.message.caption else callback.message.text
        if text_to_publish:
            text_to_publish = text_to_publish.replace("🧠 Завдання:", "").strip()
            if "⚠️ Unsplash Error:" in text_to_publish:
                 msg_parts = text_to_publish.split("\n\n", 1)
                 if len(msg_parts) > 1:
                     text_to_publish = msg_parts[1].strip()

        if platform == 'tg':
            if callback.message.photo:
                file_id = callback.message.photo[-1].file_id
                await bot.send_photo(CHANNEL_ID, photo=file_id, caption=text_to_publish[:1000])
            elif text_to_publish:
                await bot.send_message(CHANNEL_ID, text_to_publish[:4000])
            
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT quiz_data FROM telegram_plan WHERE post_date=%s AND post_time=%s", (date_str, post_time))
            row = cursor.fetchone()
            conn.close()
            if row and row[0]:
                 p = row[0].split("|")
                 await send_poll_safe(CHANNEL_ID, p[0], p[1:4], p[4])
                 
            msg = "✅ ОПУБЛІКОВАНО"
        else:
            msg = "✅ ЗАТВЕРДЖЕНО (Інста)"

        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(msg)

    except Exception as e:
        await callback.answer(f"❌ ПОМИЛКА: {str(e)}", show_alert=True)

# ЗМІНА ФОТО
@dp.callback_query(F.data.startswith("pic_"))
async def cb_pic(callback: types.CallbackQuery):
    try:
        parts = callback.data.split("_")
        platform = parts[1]
        date_str = parts[2]
        post_time = parts[3] if len(parts) > 3 else 'morning'
        table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
        
        conn = get_db_connection()
        cursor = conn.cursor()
        if platform == 'tg':
            cursor.execute(f"SELECT photo_query FROM {table_name} WHERE post_date = %s AND post_time = %s", (date_str, post_time))
        else:
            cursor.execute(f"SELECT photo_query FROM {table_name} WHERE post_date = %s", (date_str,))
        row = cursor.fetchone()
        conn.close()
        
        if row and row[0]:
            new_url, error = await get_photo_url_debug(row[0])
            
            if new_url:
                media = InputMediaPhoto(media=new_url, caption=callback.message.caption)
                await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
            else:
                await callback.answer(f"Unsplash Error: {error}", show_alert=True)
                
    except Exception as e:
        await callback.answer(f"Err: {e}", show_alert=True)

# ТЕКСТ
@dp.callback_query(F.data.startswith("txt_"))
async def cb_txt(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    platform = parts[1]
    date_str = parts[2]
    post_time = parts[3] if len(parts) > 3 else 'morning'
    table_name = "instagram_plan" if platform == 'inst' else "telegram_plan"
    
    conn = get_db_connection()
    cursor = conn.cursor()
    if platform == 'tg':
        cursor.execute(f"SELECT topic, ai_prompt, photo_query FROM {table_name} WHERE post_date = %s AND post_time = %s", (date_str, post_time))
    else:
        cursor.execute(f"SELECT topic, ai_prompt, photo_query FROM {table_name} WHERE post_date = %s", (date_str,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        topic, prompt, photo_q = row
        has_photo = photo_q is not None
        
        if platform == 'inst':
             slides_text, caption_text = await generate_carousel_texts(topic, prompt)
             await callback.message.edit_text(text=f"🖼 ТЕКСТ ДЛЯ СЛАЙДІВ ({date_str}):\n\n{slides_text}", reply_markup=callback.message.reply_markup)
             await bot.send_message(callback.message.chat.id, f"📝 ОПИС ПІД ПОСТОМ:\n\n{caption_text}")
        else:
             new_text = await generate_ai_text(topic, prompt, platform, has_photo)
             if callback.message.caption:
                 await callback.message.edit_caption(caption=new_text, reply_markup=callback.message.reply_markup)
             else:
                 await callback.message.edit_text(text=new_text, reply_markup=callback.message.reply_markup)

async def send_poll_safe(chat_id, question, options, correct_option_id):
     try:
         await bot.send_poll(chat_id=chat_id, question=question, options=options, type='quiz', correct_option_id=int(correct_option_id))
     except Exception as e:
         logging.error(f"Помилка надсилання опитування: {e}")

# --- SERVER ---
async def handle(request): return web.Response(text="Bot is Alive")

async def main():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    # ТГ Ранок (09:00)
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0, args=['tg', 'morning'])
    # Інста Ранок (09:10)
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=5, args=['inst', 'morning'])
    # ТГ Вечір (18:00)
    scheduler.add_job(prepare_draft, 'cron', hour=18, minute=0, args=['tg', 'evening'])
    scheduler.start()
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())