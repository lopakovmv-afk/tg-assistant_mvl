import os
import json
import logging
import tempfile
import requests
import redis
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
REDIS_URL = os.getenv("REDIS_URL")

openai_client = OpenAI(api_key=OPENAI_API_KEY)
SCOPES = ["https://www.googleapis.com/auth/calendar"]

try:
    redis_client = redis.from_url(REDIS_URL) if REDIS_URL else None
    if redis_client:
        redis_client.ping()
        logger.info("Redis connected!")
except Exception as e:
    logger.error(f"Redis connection error: {e}")
    redis_client = None


def get_memory(user_id):
    if not redis_client:
        return ""
    try:
        data = redis_client.get(f"memory:{user_id}")
        return data.decode() if data else ""
    except:
        return ""


def save_memory(user_id, memory_text):
    if not redis_client:
        return
    try:
        redis_client.set(f"memory:{user_id}", memory_text)
    except:
        pass


def get_history(user_id):
    if not redis_client:
        return []
    try:
        data = redis_client.get(f"history:{user_id}")
        return json.loads(data.decode()) if data else []
    except:
        return []


def save_history(user_id, history):
    if not redis_client:
        return
    try:
        redis_client.set(f"history:{user_id}", json.dumps(history[-20:]))
    except:
        pass


SYSTEM_PROMPT = """Ты — личный ИИ-ассистент Максима Лопакова.

## О МАКСИМЕ
Максим Лопаков, 31 год (06.06.1994). Предприниматель, отец троих детей. Супруга Екатерина.
База — дисциплина. Бразильское джиу-джитсу (пурпурный пояс), КМС по боевому самбо. Верующий.
Стиль — стратегический: архитектура бизнеса, слабые места, решения через цифры. Не про операционку.
Решения: цифры + обсуждение + интуиция. Двигается от интуитивного к системному управлению.
Глобальная цель: стать первым долларовым миллионером в семье, не переступив через моральные принципы.
Активно изучает ИИ для оптимизации процессов.

## ПОРТФЕЛЬ БИЗНЕСОВ

### ВЕЗИБЕНЗИН
Цифровой сервис комплексного топливообеспечения предприятий (B2B).
Полный цикл: поставка → хранение → заправка → контроль → аналитика → сервис.
Продукты: бесплатная установка КАЗС, мобильные бустеры, оптовые поставки, комбо КАЗС+Бустер.
Целевые отрасли: ТКО, горнодобыча, производство, FMCG/логистика.
Инфраструктура: нефтебазы с ж/д тупиками от Сибири до Центральной России.

### ТЗРН
Завод в Томске. Контрактный производитель безалкогольных напитков (СТМ).
Модель: давальческое сырьё — заказчик привозит ингредиенты и рецептуру, ТЗРН варит, разливает, упаковывает, отгружает.
Мощность: варочный порядок 2000 л, линия розлива 500 банок/час.
Целевая цена СТМ: 15-25 руб/единица.
Стратегия: ТЗРН = производственная инфраструктура для брендов, не самостоятельный продавец.

### ВЫНЕСИ ПОПИТЬ
Бренд натуральных лимонадов. В процессе выделения в отдельную компанию.
Новая структура: другой учредительный состав + инфлюенсер.
Модель: компания-бренд без собственных мощностей — развивает бренды и загружает контрактные производства (в т.ч. ТЗРН).
Позиционирование: без сахара, без подсластителей, массовый рынок, вкус как драйвер повторных покупок.

### HUMANTECH
Оператор производственных процессов для предприятий ТКО. Не аутстаффинг.
Продукт: управляемая производственная мощность объекта сортировки ТКО.
KPI: выполнение производственного плана, объём выборки, стабильность смен.
Учредители: Максим (архитектура, система, KPI), Эмиль (стратегия, развитие, переговоры), Артур (операционка), Виталий.
Стратегическая цель: федеральная платформа ТКО → подготовка к продаже как инвестиционного актива.
Команда: Ильдар (финансы), Артур (операции), Виталий, Ксения (координатор), Евгения (привлечение).

## СТИЛЬ РАБОТЫ
- Гибкий график, 1-3 встречи/звонка в день
- Контроль через цифры и личные встречи
- Ориентирован на результат
- Предпочитает чёткие структурированные ответы без воды

## КАК ОТВЕЧАТЬ
- Обращайся на "ты"
- Чётко и структурированно, без лишних слов
- Конкретные действия и выводы
- Если видишь системную проблему или слабое место — скажи прямо

## ФОРМАТЫ (только JSON, без лишнего текста):
Встреча: {"action":"create_event","title":"...","date":"YYYY-MM-DD","time":"HH:MM","duration_minutes":60,"description":"..."}
Задача: {"action":"add_task","title":"...","eisenhower":"Q1/Q2/Q3/Q4","due":"YYYY-MM-DD или null"}
Запомнить: {"action":"remember","fact":"..."}
Всё остальное — текст, чётко и по делу.

МАТРИЦА ЭЙЗЕНХАУЭРА: Q1=Срочно+Важно, Q2=Не срочно+Важно, Q3=Срочно+Не важно, Q4=Не срочно+Не важно

Сегодня: """ + datetime.now().strftime("%Y-%m-%d")


def add_todoist_task(title, priority=2, due_date=None):
    try:
        if not TODOIST_TOKEN:
            return False, "Todoist не подключён"
        payload = {"content": title, "priority": priority}
        if due_date:
            payload["due_date"] = due_date
        response = requests.post(
            "https://api.todoist.com/api/v1/tasks",
            headers={"Authorization": f"Bearer {TODOIST_TOKEN}"},
            json=payload
        )
        if response.status_code == 200:
            return True, response.json().get("url", "")
        else:
            return False, f"Ошибка {response.status_code}"
    except Exception as e:
        return False, str(e)


def get_calendar_service():
    try:
        if not GOOGLE_CREDENTIALS:
            return None
        creds_data = json.loads(GOOGLE_CREDENTIALS)
        creds = service_account.Credentials.from_service_account_info(creds_data, scopes=SCOPES)
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


async def process_with_gpt(text, history, memory):
    history.append({"role": "user", "content": text})
    system = SYSTEM_PROMPT
    if memory:
        system += f"\n\n## ЧТО ЗАПОМНЕНО О МАКСИМЕ:\n{memory}"
    messages = [{"role": "system", "content": system}] + history[-10:]
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini", messages=messages, max_tokens=500
    )
    reply = response.choices[0].message.content.strip()
    history.append({"role": "assistant", "content": reply})
    return reply, history


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.message.reply_text(
        "👋 Привет, Максим!\n\n"
        "Я знаю твои бизнесы и готов помогать.\n\n"
        "Умею:\n"
        "🎤 Голосовые сообщения\n"
        "📅 Встречи в Google Calendar\n"
        "✅ Задачи в Todoist\n"
        "🧠 Запоминать важное\n"
        "📊 Матрица Эйзенхауэра\n\n"
        "Говори что нужно."
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
        await update.message.reply_text(f"📝 *{text}*", parse_mode="Markdown")
        await handle_text_logic(update, context, text)
    finally:
        os.unlink(tmp_path)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await handle_text_logic(update, context, update.message.text)


async def handle_text_logic(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    history = get_history(user_id)
    memory = get_memory(user_id)

    await update.message.reply_text("⏳")
    reply, history = await process_with_gpt(text, history, memory)
    save_history(user_id, history)

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
                    f"✅ Встреча создана!\n"
                    f"📅 *{data.get('title')}*\n"
                    f"🕐 {data.get('date')} в {data.get('time')}\n"
                    f"🔗 [Открыть]({result})",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(f"⚠️ Ошибка Calendar: {result}")

        elif action == "add_task":
            labels = {
                "Q1": "🔴 Q1 — делай немедленно",
                "Q2": "🟡 Q2 — запланируй",
                "Q3": "🔵 Q3 — делегируй",
                "Q4": "⚪ Q4 — удали"
            }
            priority_map = {"Q1": 4, "Q2": 3, "Q3": 2, "Q4": 1}
            q = data.get("eisenhower", "Q2")
            due = data.get("due")
            success, url = add_todoist_task(
                title=data.get("title"),
                priority=priority_map.get(q, 3),
                due_date=due if due and due != "null" else None
            )
            due_text = f"\n📆 {due}" if due and due != "null" else ""
            if success:
                await update.message.reply_text(
                    f"✅ Задача в Todoist!\n*{data.get('title')}*{due_text}\n{labels.get(q, q)}",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(f"⚠️ Ошибка Todoist: {url}")

        elif action == "remember":
            fact = data.get("fact", "")
            current = get_memory(user_id)
            new_memory = current + f"\n- {fact}" if current else f"- {fact}"
            save_memory(user_id, new_memory)
            await update.message.reply_text(f"🧠 Запомнил: *{fact}*", parse_mode="Markdown")

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
