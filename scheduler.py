from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timedelta
import pytz

ROME_TZ = pytz.timezone("Europe/Rome")

class ReminderScheduler:
    def __init__(self, bot_send_callable):
        # bot_send_callable: funzione async che invia un messaggio Telegram (chat_id, text)
        self.scheduler = AsyncIOScheduler(timezone=ROME_TZ)
        self.bot_send = bot_send_callable

    def start(self):
        if not self.scheduler.running:
            self.scheduler.start()

    def schedule_event_reminder(self, chat_id: int, title: str, event_ts: int):
        """
        Pianifica un promemoria 10 minuti prima dell'evento.
        - event_ts è in secondi UTC (UNIX timestamp)
        """
        event_dt = datetime.fromtimestamp(event_ts, tz=pytz.UTC).astimezone(ROME_TZ)
        remind_dt = event_dt - timedelta(minutes=10)
        now = datetime.now(ROME_TZ)
        if remind_dt <= now:
            # Se l'orario del promemoria è già passato, non pianifico nulla
            return
        text = f"⏰ Promemoria: '{title}' il {event_dt.strftime('%d/%m/%Y %H:%M')}"
        self.scheduler.add_job(
            self.bot_send,
            trigger=DateTrigger(run_date=remind_dt),
            args=[chat_id, text],
            misfire_grace_time=60,  # se 'salta' l'orario per pochi secondi, manda comunque
            coalesce=True,
            max_instances=3,
        )
