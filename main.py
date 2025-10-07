import os
import asyncio
from datetime import datetime
import pytz
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from db import init_db, add_event, list_all_future, find_candidates_by_title, update_event_time, remove_event
from nlp import detect_intent, extract_datetime, strip_date_from_title, extract_move_targets, extract_remove_target, INTENT_ADD, INTENT_RECAP, INTENT_REMOVE, INTENT_MOVE, INTENT_HELP
from scheduler import ReminderScheduler

ROME_TZ = pytz.timezone("Europe/Rome")

async def scheduler_send(chat_id: int, text: str):
    # usato dallo scheduler per inviare i promemoria
    await GLOBAL_APP.bot.send_message(chat_id=chat_id, text=text)

GLOBAL_APP = None
REM_SCHED = None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ciao! Sono Self Me AI — Agenda. Scrivimi in italiano per aggiungere eventi, fare il recap, modificare o rimuovere.\n\n"
        "Esempi:\n"
        "• Metti in agenda domani alle 15 riunione budget\n"
        "• Recap agenda\n"
        "• Sposta riunione budget a lunedì alle 10\n"
        "• Rimuovi visita commercialista\n"
    )

def fmt_event_line(title: str, ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=pytz.UTC).astimezone(ROME_TZ)
    date_str = dt.strftime("%a %d/%m/%Y %H:%M")
    return f"• {date_str} — {title}"

async def handle_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text

    dt = extract_datetime(text, now_dt=datetime.now(ROME_TZ))
    if not dt:
        await update.message.reply_text("Non ho capito la data/ora. Puoi ripetere? (es. 'venerdì alle 10')")
        return

    title = strip_date_from_title(text) or "Evento"
    start_ts = int(dt.astimezone(pytz.UTC).timestamp())
    add_event(user_id, chat_id, title, start_ts)
    REM_SCHED.schedule_event_reminder(chat_id, title, start_ts)
    await update.message.reply_text(f"✅ Aggiunto: {title} — {dt.strftime('%d/%m/%Y %H:%M')}")

async def handle_recap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now_ts = int(datetime.now(pytz.UTC).timestamp())
    events = list_all_future(user_id, now_ts)
    if not events:
        await update.message.reply_text("Agenda vuota da adesso in poi. ✨")
        return
    lines = ["🗓️ *Prossimi impegni*:", ""]
    for _id, title, start_ts in events[:50]:
        lines.append(fmt_event_line(title, start_ts))
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def handle_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text

    title_guess, dt = extract_remove_target(text)
    now_ts = int(datetime.now(pytz.UTC).timestamp())

    candidates = []
    if title_guess:
        candidates = find_candidates_by_title(user_id, title_guess, now_ts)
    if not candidates and dt:
        # se non c'è un match per titolo, prova a cercare per orario vicino ±30m
        t0 = int(dt.astimezone(pytz.UTC).timestamp())
        all_upcoming = list_all_future(user_id, now_ts)
        for _id, title, start_ts in all_upcoming:
            if abs(start_ts - t0) <= 1800:
                candidates.append((_id, title, start_ts))

    if not candidates:
        await update.message.reply_text("Non ho trovato eventi da rimuovere. Prova specificando titolo o orario.")
        return

    event_id, title, start_ts = candidates[0]
    remove_event(event_id)
    await update.message.reply_text(f"🗑️ Rimosso: {title} — {fmt_event_line(title, start_ts)[2:]}")

async def handle_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text

    title_guess, new_dt = extract_move_targets(text)
    if not new_dt:
        await update.message.reply_text("Non ho capito la nuova data/ora. Riprova es. 'sposta ... a martedì alle 11'.")
        return

    now_ts = int(datetime.now(pytz.UTC).timestamp())
    candidates = []
    if title_guess:
        candidates = find_candidates_by_title(user_id, title_guess, now_ts)

    if not candidates:
        await update.message.reply_text("Non ho trovato quale evento spostare. Specifica meglio il titolo.")
        return

    event_id, title, old_ts = candidates[0]
    new_ts = int(new_dt.astimezone(pytz.UTC).timestamp())
    update_event_time(event_id, new_ts)
    REM_SCHED.schedule_event_reminder(chat_id, title, new_ts)

    old_line = fmt_event_line(title, old_ts)
    new_line = fmt_event_line(title, new_ts)
    await update.message.reply_text(f"🔁 Spostato:\n~{old_line}~\n→ {new_line}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def fallback_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    intent = detect_intent(text)

    if intent == INTENT_ADD:
        await handle_add(update, context); return
    if intent == INTENT_RECAP:
        await handle_recap(update, context); return
    if intent == INTENT_REMOVE:
        await handle_remove(update, context); return
    if intent == INTENT_MOVE:
        await handle_move(update, context); return
    if intent == INTENT_HELP:
        await help_cmd(update, context); return

    await update.message.reply_text("Ok! Dimmi se vuoi che *metta in agenda*, faccia un *recap*, *sposti* o *rimuova* qualcosa.")

def bootstrap_scheduler(app: Application) -> ReminderScheduler:
    scheduler = ReminderScheduler(bot_send_callable=scheduler_send)
    scheduler.start()
    return scheduler

from db import get_conn  # <-- assicurati che sia presente in cima al file

def schedule_existing_reminders():
    # usa lo stesso DB di db.py (Render Disk /var/data/data.sqlite)
    now_ts = int(datetime.now(pytz.UTC).timestamp())
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id, chat_id, title, start_ts FROM events WHERE start_ts>=? ORDER BY start_ts ASC",
            (now_ts,),
        )
        rows = cur.fetchall()
        for event_id, chat_id, title, start_ts in rows:
            REM_SCHED.schedule_event_reminder(chat_id, title, start_ts)


def main():
    global GLOBAL_APP, REM_SCHED
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN mancante nel file .env")
    init_db()

    application = Application.builder().token(token).build()
    GLOBAL_APP = application
    REM_SCHED = bootstrap_scheduler(application)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_chat))

    schedule_existing_reminders()

    print("✅ Self Me AI — Agenda Bot avviato. Timezone:", os.getenv("TZ", "Europe/Rome"))
    application.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
