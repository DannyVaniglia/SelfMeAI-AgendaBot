import os
from datetime import datetime
import pytz
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

from rapidfuzz import fuzz

# DB & NLP
from db import (
    init_db,
    get_conn,
    add_event,
    list_all_future,
    find_candidates_by_title,
    update_event_time,
    remove_event,
)
from nlp import (
    detect_intent,
    extract_datetime,
    strip_date_from_title,
    extract_move_targets,
    extract_remove_target,
    INTENT_ADD,
    INTENT_RECAP,
    INTENT_REMOVE,
    INTENT_MOVE,
    INTENT_HELP,
)
from scheduler import ReminderScheduler

# -------------------- Costanti & util --------------------

PENDING_KEY = "pending_action"
ROME_TZ = pytz.timezone("Europe/Rome")


def now_utc_ts() -> int:
    """Epoch secondi in UTC (timezone-aware)."""
    return int(datetime.now(pytz.UTC).timestamp())


def fmt_event_line(title: str, ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=pytz.UTC).astimezone(ROME_TZ)
    date_str = dt.strftime("%a %d/%m/%Y %H:%M")
    return f"• {date_str} — {title}"


def find_best_matches(user_id: int, query: str, now_ts: int, limit: int = 5):
    """Ritorna i migliori match per titolo usando RapidFuzz (≥60)."""
    events = list_all_future(user_id, now_ts)
    scored = []
    q = (query or "").lower().strip()
    for eid, title, start_ts in events:
        score = fuzz.partial_ratio(q, title.lower())
        if score >= 60:
            scored.append((score, eid, title, start_ts))
    scored.sort(reverse=True)
    return [(eid, title, start_ts) for score, eid, title, start_ts in scored[:limit]]


# -------------------- Globals --------------------

GLOBAL_APP = None
REM_SCHED: ReminderScheduler | None = None


async def scheduler_send(chat_id: int, text: str):
    """Usato dallo scheduler per inviare i promemoria."""
    await GLOBAL_APP.bot.send_message(chat_id=chat_id, text=text)


# -------------------- Handlers --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ciao! Sono Self Me AI — Agenda. Scrivimi in italiano per aggiungere eventi, fare il recap, modificare o rimuovere.\n\n"
        "Esempi:\n"
        "• Metti in agenda domani alle 15 riunione budget\n"
        "• Recap agenda\n"
        "• Sposta riunione budget a lunedì alle 10\n"
        "• Rimuovi visita commercialista\n"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


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
    now_ts = now_utc_ts()
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
    now_ts = now_utc_ts()

    candidates = []
    if title_guess:
        # Prima fuzzy, poi LIKE come fallback
        candidates = find_best_matches(user_id, title_guess, now_ts)
        if not candidates:
            candidates = find_candidates_by_title(user_id, title_guess, now_ts)

    if not candidates and dt:
        t0 = int(dt.astimezone(pytz.UTC).timestamp())
        all_upcoming = list_all_future(user_id, now_ts)
        for _id, title, start_ts in all_upcoming:
            if abs(start_ts - t0) <= 1800:
                candidates.append((_id, title, start_ts))

    if not candidates:
        await update.message.reply_text("Non ho trovato eventi da rimuovere. Specifica meglio il titolo o l’orario.")
        return
    
    # Disambiguazione: se più di 1 candidato, chiedi numero
    if len(candidates) > 1:
        context.user_data[PENDING_KEY] = {
            "type": "remove",
            "candidates": candidates,
        }
        lines = [
            "Ho trovato più eventi. Quale intendi rimuovere? Rispondi con *1-{}*:".format(min(5, len(candidates))),
            "",
        ]
        for i, (_id, title, start_ts) in enumerate(candidates[:5], start=1):
            lines.append(f"{i}) {fmt_event_line(title, start_ts)[2:]}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    # Un solo candidato → rimuovi subito
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

    now_ts = now_utc_ts()
    candidates = []
    if title_guess:
        candidates = find_best_matches(user_id, title_guess, now_ts)
        if not candidates:
            candidates = find_candidates_by_title(user_id, title_guess, now_ts)

    if not candidates:
        await update.message.reply_text("Non ho trovato quale evento spostare. Specifica meglio il titolo.")
        return

    if len(candidates) > 1:
        # Salva pending + nuova data
        context.user_data[PENDING_KEY] = {
            "type": "move",
            "candidates": candidates,
            "new_ts": int(new_dt.astimezone(pytz.UTC).timestamp()),
        }
        lines = ["Quale evento vuoi spostare? Rispondi con *1-{}*:".format(min(5, len(candidates))), ""]
        for i, (_id, title, start_ts) in enumerate(candidates[:5], start=1):
            lines.append(f"{i}) {fmt_event_line(title, start_ts)[2:]}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    # 1 solo candidato → sposta subito
    event_id, title, old_ts = candidates[0]
    new_ts = int(new_dt.astimezone(pytz.UTC).timestamp())
    update_event_time(event_id, new_ts)
    REM_SCHED.schedule_event_reminder(chat_id, title, new_ts)
    old_line = fmt_event_line(title, old_ts)
    new_line = fmt_event_line(title, new_ts)
    await update.message.reply_text(f"🔁 Spostato:\n~{old_line}~\n→ {new_line}")


async def handle_numeric_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (update.message.text or "").strip()
    if not msg.isdigit():
        return
    if PENDING_KEY not in context.user_data:
        return

    choice = int(msg)
    pending = context.user_data[PENDING_KEY]
    candidates = pending.get("candidates", [])
    if choice < 1 or choice > min(5, len(candidates)):
        await update.message.reply_text("Scelta non valida. Rispondi con un numero della lista.")
        return

    event_id, title, ts = candidates[choice - 1]
    typ = pending.get("type")
    chat_id = update.effective_chat.id

    if typ == "remove":
        remove_event(event_id)
        await update.message.reply_text(f"🗑️ Rimosso: {title} — {fmt_event_line(title, ts)[2:]}")
    elif typ == "move":
        new_ts = pending.get("new_ts")
        if not new_ts:
            await update.message.reply_text("Non ho capito la nuova data/ora, riprova con 'sposta ... a ...'.")
            context.user_data.pop(PENDING_KEY, None)
            return
        update_event_time(event_id, new_ts)
        REM_SCHED.schedule_event_reminder(chat_id, title, new_ts)
        old_line = fmt_event_line(title, ts)
        new_line = fmt_event_line(title, new_ts)
        await update.message.reply_text(f"🔁 Spostato:\n~{old_line}~\n→ {new_line}")

    # pulisci stato
    context.user_data.pop(PENDING_KEY, None)


async def fallback_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Router intent → handler specifico."""
    text = (update.message.text or "").strip()
    intent = detect_intent(text)

    if intent == INTENT_ADD:
        await handle_add(update, context)
        return
    if intent == INTENT_RECAP:
        await handle_recap(update, context)
        return
    if intent == INTENT_REMOVE:
        await handle_remove(update, context)
        return
    if intent == INTENT_MOVE:
        await handle_move(update, context)
        return
    if intent == INTENT_HELP:
        await help_cmd(update, context)
        return

    await update.message.reply_text(
        "Ok! Dimmi se vuoi che *metta in agenda*, faccia un *recap*, *sposti* o *rimuova* qualcosa."
    )


# -------------------- Scheduler bootstrap --------------------

def bootstrap_scheduler(app: Application) -> ReminderScheduler:
    scheduler = ReminderScheduler(bot_send_callable=scheduler_send)
    scheduler.start()
    return scheduler


def schedule_existing_reminders():
    """All’avvio, riprogramma i promemoria per tutti gli eventi futuri."""
    now_ts = now_utc_ts()
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id, chat_id, title, start_ts FROM events WHERE start_ts>=? ORDER BY start_ts ASC",
            (now_ts,),
        )
        rows = cur.fetchall()
        for event_id, chat_id, title, start_ts in rows:
            REM_SCHED.schedule_event_reminder(chat_id, title, start_ts)


# -------------------- Entrypoint --------------------

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

    # Comandi
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))

    # Prima il selettore numerico (più specifico), poi il fallback su testo generico
    application.add_handler(MessageHandler(filters.Regex(r"^[1-5]$"), handle_numeric_choice))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_chat))

    # Ripristina promemoria esistenti
    print("📍 boot: prima di schedule_existing_reminders()")
    schedule_existing_reminders()
    print("📍 boot: dopo schedule_existing_reminders()")

    print("✅ Self Me AI — Agenda Bot avviato. Timezone:", os.getenv("TZ", "Europe/Rome"))
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

