import os
import json
import logging
import tempfile
import requests
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
TODOIST_TOKEN = os.getenv("TODOIST_TOKEN")

openai_client = OpenAI(api_key=OPENAI_API_KEY)
SCOPES = ["https://www.googleapis.com/auth/calendar"]

SYSTEM_PROMPT = """Ты — личный ИИ-ассистент. Помогаешь управлять расписанием, задачами и планированием.

Ты умеешь:
- Добавлять задачи в Todoist
- Создавать встречи в Google Calendar
- Планировать день, неделю и месяц
- Расставлять приоритеты по матрице Эйзенхауэра:
  Q1 = Срочно + Важно (делай немедленно) → приоритет 1
  Q2 = Не срочно + Важно (планируй) → приоритет 2
  Q3 = Срочно + Не важно (делегируй) → приоритет 3
  Q4 = Не срочно + Не важно (удали) → приоритет 4

Если пользователь хочет добавить задачу — отвечай ТОЛЬКО в таком JSON:
{"action": "add_task", "title": "...", "eisenhower": "Q1/Q2/Q3/Q4", "due": "YYYY-MM-DD или null"}

Если пользователь хочет создать встречу/событие — отвечай ТОЛЬКО в таком JSON:
{"action": "create_event", "title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "duration_minutes": 60, "description": "..."}

Если пользователь просит показать план или просто общается — отвечай обычным текстом.

ВАЖНО: date всегда в формате YYYY-MM-DD, time в формате HH:MM. Если сказано "завтра" — используй завтрашнюю дату.
Сегодня: """ + datetime.now().strftime("%Y-%m-%d")


def add_todoist_task(title, priority=2, due_date=None):
    try:
        if not TODOIST_TOKEN:
            return False, "Todoist не подключён"
        
        payload = {
            "content": title,
            "priority": priority
        }
        if due_date:
            payload["due_date"] = due_date

        response = requests.post(
            "https://api.todoist.com/api/v1/tasks",
            headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
            json=payload
        )
        
        if response.status_code == 200:
            task = response.json()
            return True, task.get("url", "")
        else:
            return False, f"Ошибка {response.status_code}"
    except Exception as e:
        logger.error(f"Todoist error: {e}")
        return False, str(e)


def get_calendar_service():
    try:
        if not GOOGLE_CREDENTIALS:
            logger.error("GOOGLE_CREDENTIALS not set")
            return None
        creds_data = json.loads(GOOGLE_CREDENTIALS)
        creds = service_account.Credentials.from_service_account_info(
            creds_data, scopes=SCOPES
        )
        return build("calendar", "v3", credentials=creds)
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

        logger.info(f"Creating event: {title} on {date} at {time}")
        result = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
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
        "✅ Добавлять задачи в Todoist\n"
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
            priority_map = {"Q1": 4, "Q2": 3, "Q3": 2, "Q4": 1}
            q = data.get("eisenhower", "Q2")
            due = data.get("due")

            success, url = add_todoist_task(
                title=data.get("title"),
                priority=priority_map.get(q, 3),
                due_date=due if due and due != "null" else None
            )

            due_text = f"\n📆 Когда: {due}" if due and due != "null" else ""
            if success:
                await update.message.reply_text(
                    f"✅ Задача добавлена в Todoist!\n"
                    f"*{data.get('title')}*{due_text}\n\n"
                    f"📊 {labels.get(q, q)}",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(f"⚠️ Ошибка Todoist: {url}")
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
