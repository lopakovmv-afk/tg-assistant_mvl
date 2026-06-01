import os
import json
import logging
import tempfile
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from openai import OpenAI
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

openai_client = OpenAI(api_key=OPENAI_API_KEY)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

SYSTEM_PROMPT = """Ты — личный ИИ-ассистент. Твоя задача — помогать управлять расписанием, задачами и планированием.

Ты умеешь:
- Добавлять задачи и напоминания
- Создавать встречи в Google Calendar
- Планировать день, неделю и месяц
- Расставлять приоритеты по матрице Эйзенхауэра (Срочно/Важно, Не срочно/Важно, Срочно/Не важно, Не срочно/Не важно)
- Распознавать голосовые сообщения

Когда пользователь просит создать встречу или задачу — извлеки из его сообщения:
- Название
- Дату и время (если есть)
- Описание (если есть)

Если нужно создать событие в календаре, ответь СТРОГО в таком JSON формате (без лишнего текста):
{"action": "create_event", "title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "duration_minutes": 60, "description": "..."}

Если нужно показать план или расставить приоритеты — отвечай обычным текстом.
Если нужно просто добавить задачу в список — отвечай:
{"action": "add_task", "title": "...", "priority": "high/medium/low", "eisenhower": "Q1/Q2/Q3/Q4"}

Q1 = Срочно + Важно, Q2 = Не срочно + Важно, Q3 = Срочно + Не важно, Q4 = Не срочно + Не важно

Сегодняшняя дата: """ + datetime.now().strftime("%Y-%m-%d %A")


def get_google_calendar_service():
    creds = None
    token_file = "token.pickle"
    creds_file = "credentials.json"

    if os.path.exists(token_file):
        with open(token_file, "rb") as token:
            creds = pickle.load(token)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(creds_file):
                return None
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "wb") as token:
            pickle.dump(creds, token)

    return build("calendar", "v3", credentials=creds)


def create_calendar_event(title, date, time, duration_minutes=60, description=""):
    try:
        service = get_google_calendar_service()
        if not service:
            return False, "Google Calendar не подключён"

        start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        from datetime import timedelta
        end_dt = start_dt + timedelta(minutes=duration_minutes)

        event = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "Europe/Berlin"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "Europe/Berlin"},
        }

        event = service.events().insert(calendarId="primary", body=event).execute()
        return True, event.get("htmlLink")
    except Exception as e:
        return False, str(e)


async def transcribe_voice(file_path):
    with open(file_path, "rb") as audio_file:
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ru"
        )
    return transcript.text


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "👋 Привет! Я твой личный ассистент.\n\n"
        "Я умею:\n"
        "🎤 Распознавать голосовые сообщения\n"
        "📅 Создавать встречи в Google Calendar\n"
        "✅ Добавлять задачи с приоритетами\n"
        "🗓 Планировать день, неделю и месяц\n"
        "📊 Расставлять приоритеты по матрице Эйзенхауэра\n\n"
        "Просто напиши или надиктуй что нужно сделать!"
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    await update.message.reply_text("🎤 Распознаю голосовое сообщение...")

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
    if "conversation_history" not in context.user_data:
        context.user_data["conversation_history"] = []

    await update.message.reply_text("⏳ Думаю...")

    reply = await process_with_gpt(text, context.user_data["conversation_history"])

    # Try to parse as JSON action
    try:
        data = json.loads(reply.strip())
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
                await update.message.reply_text(f"❌ Ошибка создания события: {result}")

        elif action == "add_task":
            eisenhower_labels = {
                "Q1": "🔴 Срочно + Важно",
                "Q2": "🟡 Не срочно + Важно",
                "Q3": "🔵 Срочно + Не важно",
                "Q4": "⚪ Не срочно + Не важно"
            }
            q = data.get("eisenhower", "Q2")
            label = eisenhower_labels.get(q, q)
            await update.message.reply_text(
                f"✅ Задача добавлена!\n"
                f"📌 *{data.get('title')}*\n"
                f"📊 Матрица Эйзенхауэра: {label}",
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
    app.run_polling()


if __name__ == "__main__":
    main()
