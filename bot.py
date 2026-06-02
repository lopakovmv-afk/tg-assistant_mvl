import os
import json
import logging
import tempfile
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
CALENDAR_ID = os.getenv("CALENDAR_ID", "primary")

openai_client = OpenAI(api_key=OPENAI_API_KEY)
SCOPES = ["https://www.googleapis.com/auth/calendar"]

SYSTEM_PROMPT = """Ты — личный ИИ-ассистент. Помогаешь управлять расписанием, задачами и планированием.

Ты умеешь:
- Добавлять задачи и напоминания
- Создавать встречи в Google Calendar
- Планировать день, неделю и месяц
- Расставлять приоритеты по матрице Эйзенхауэра:
  Q1 = Срочно + Важно (делай немедленно)
  Q2 = Не срочно + Важно (планируй)
  Q3 = Срочно + Не важно (делегируй)
  Q4 = Не срочно + Не важно (удали)

Если пользователь хочет добавить задачу — отвечай ТОЛЬКО в таком JSON без лишнего текста:
{"action": "add_task", "title": "...", "eisenhower": "Q1/Q2/Q3/Q4", "due": "когда (если указано)"}

Если пользователь хочет создать встречу или событие — отвечай ТОЛЬКО в таком JSON без лишнего текста:
{"action": "create_event", "title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "duration_minutes": 60, "description": "..."}

ВАЖНО: date всегда в формате YYYY-MM-DD, time в формате HH:MM. Если сказано "завтра" — используй завтрашнюю дату.
Сегодня: """ + datetime.now().strftime("%Y-%m-%d")


def get_calendar_service():
    try:
        if not GOOGLE_CREDENTIALS:
            logger.error("GOOGLE_CREDENTIALS not set")
            return None
        creds_data = json.loads(GOOGLE_CREDENTIALS)
        creds = service_account.Credentials.from_service_account_info(
            creds_data, scopes=SCOPES
        )
        service = build("calendar", "v3", credentials=creds)
        logger.info("Calendar service created successfully")
        return service
    except Exception as e:
        logger.error(f"Calendar auth error: {e}")
        return None


def create_calendar_event(title, date, time, duration_minutes=60, description=""):
    try:
        service = get_calendar_service()
        if not service:
            return False, "Не удалось подключиться к Google Calendar"

        start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(minutes=int(duration_minutes))

        event = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Moscow"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Moscow"},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 60},
                    {"method": "popup", "minutes": 10},
                ],
            },
        }

        logger.info(f"Creating event: {title} on {date} at {time} in calendar {CALENDAR_ID}")
        result = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        logger.info(f"Event created: {result.get('htmlLink')}")
        return True, result.get("htmlLink")
    except Exception as e:
        logger.error(f"Calendar event error: {e}")
        return False, str(e)


async def transcribe_voice(file_path):
    with open(file_path, "rb") as audio_file:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1", file=audio_file, language="ru"
        )
    return transcript.text


async def process_with_gpt(text, conversation_history):
    conversation_history.append({"role": "user", "content": text})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history[-10:]
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini", messages=messages, max_tokens=500
    )
    reply = response.choices[0].message.content.strip()
    conversation_history.append({"role": "assistant", "content": reply})
    logger.info(f"GPT reply: {reply}")
    return reply


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "👋 Привет! Я твой личный ИИ-ассистент.\n\n"
        "Я умею:\n"
        "🎤 Распознавать голосовые сообщения\n"
        "📅 Создавать встречи в Google Calendar\n"
        "✅ Добавлять задачи с приоритетами\n"
        "📊 Матрица Эйзенхауэра\n"
        "🗓 Планировать день, неделю и месяц\n\n"
        "Просто напиши или надиктуй что нужно сделать!"
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
        # Clean up possible markdown code blocks
        clean = reply.strip().strip("```json").strip("```").strip()
        data = json.loads(clean)
        action = data.get("action")

        if action == "create_event":
            success, result = create_calendar_event(
                title=data.get("title", "Встреча"),
                date=data.get("date", datetime.now().strftime("%Y-%m-%d")),
                time=data.get("time", "10:00"),
                duration_minutes=data.get("duration_minutes", 60),
                description=data.get("description", "")
            )
            if success:
                await update.message.reply_text(
                    f"✅ Встреча создана в Google Calendar!\n"
                    f"📅 *{data.get('title')}*\n"
                    f"🕐 {data.get('date')} в {data.get('time')}\n"
                    f"🔗 [Открыть в календаре]({result})",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(f"⚠️ Ошибка Calendar: {result}")

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
                f"✅ Задача добавлена!\n*{data.get('title')}*{due}\n\n📊 {labels.get(q, q)}",
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
