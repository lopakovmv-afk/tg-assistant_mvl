[bot.py](https://github.com/user-attachments/files/28458836/bot.py)
import os
import json
import logging
import tempfile
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

openai_client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """Ты — личный ИИ-ассистент. Помогаешь управлять расписанием, задачами и планированием.

Ты умеешь:
- Добавлять задачи и напоминания
- Планировать день, неделю и месяц
- Расставлять приоритеты по матрице Эйзенхауэра:
  Q1 = Срочно + Важно (делай немедленно)
  Q2 = Не срочно + Важно (планируй)
  Q3 = Срочно + Не важно (делегируй)
  Q4 = Не срочно + Не важно (удали)

Если пользователь хочет добавить задачу — отвечай ТОЛЬКО в таком JSON формате:
{"action": "add_task", "title": "...", "eisenhower": "Q1/Q2/Q3/Q4", "due": "когда (если указано)"}

Если пользователь хочет создать встречу/событие — отвечай ТОЛЬКО в таком JSON формате:
{"action": "create_event", "title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "duration_minutes": 60, "description": "..."}

Если пользователь просит показать план, расставить приоритеты, или просто общается — отвечай обычным текстом.

Сегодня: """ + datetime.now().strftime("%Y-%m-%d, %A")


async def process_with_gpt(text, conversation_history):
    conversation_history.append({"role": "user", "content": text})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history[-10:]
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=1000
    )
    reply = response.choices[0].message.content
    conversation_history.append({"role": "assistant", "content": reply})
    return reply


async def transcribe_voice(file_path):
    with open(file_path, "rb") as audio_file:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ru"
        )
    return transcript.text


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "👋 Привет! Я твой личный ИИ-ассистент.\n\n"
        "Я умею:\n"
        "🎤 Распознавать голосовые сообщения\n"
        "✅ Добавлять задачи с приоритетами\n"
        "📊 Матрица Эйзенхауэра\n"
        "📅 Планировать день, неделю и месяц\n"
        "💬 Просто напиши или надиктуй что нужно сделать!"
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text("🎤 Распознаю...")
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name
    try:
        text = await transcribe_voice(tmp_path)
        await update.message.reply_text(f"📝 Распознано: *{text}*", parse_mode="Markdown")
        await handle_text_logic(update, context, text)
    finally:
        os.unlink(tmp_path)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await handle_text_logic(update, context, update.message.text)


async def handle_text_logic(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if "history" not in context.user_data:
        context.user_data["history"] = []
    await update.message.reply_text("⏳ Думаю...")
    reply = await process_with_gpt(text, context.user_data["history"])

    try:
        data = json.loads(reply.strip())
        action = data.get("action")

        if action == "create_event":
            msg = (
                f"📅 Встреча запланирована!\n"
                f"*{data.get('title')}*\n"
                f"🗓 {data.get('date')} в {data.get('time')}\n"
                f"⏱ {data.get('duration_minutes', 60)} минут"
            )
            if data.get("description"):
                msg += f"\n📝 {data.get('description')}"
            msg += "\n\n_(Скоро добавлю интеграцию с Google Calendar)_"
            await update.message.reply_text(msg, parse_mode="Markdown")

        elif action == "add_task":
            labels = {
                "Q1": "🔴 Срочно + Важно — делай немедленно",
                "Q2": "🟡 Не срочно + Важно — запланируй",
                "Q3": "🔵 Срочно + Не важно — делегируй",
                "Q4": "⚪ Не срочно + Не важно — удали"
            }
            q = data.get("eisenhower", "Q2")
            due = f"\n📆 Когда: {data.get('due')}" if data.get("due") else ""
            await update.message.reply_text(
                f"✅ Задача добавлена!\n"
                f"*{data.get('title')}*{due}\n\n"
                f"📊 {labels.get(q, q)}",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(reply)
    except (json.JSONDecodeError, ValueError):
        await update.message.reply_text(reply)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
