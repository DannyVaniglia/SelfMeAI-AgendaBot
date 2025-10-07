import re
from typing import Optional, Tuple
from dateparser.search import search_dates
from datetime import datetime
import pytz

ROME_TZ = pytz.timezone("Europe/Rome")

# Etichette di intento
INTENT_ADD = "add"
INTENT_RECAP = "recap"
INTENT_REMOVE = "remove"
INTENT_MOVE = "move"
INTENT_HELP = "help"
INTENT_UNKNOWN = "unknown"

# Parole chiave in italiano
ADD_KWS = ["metti in agenda", "in agenda", "in calendario", "aggiungi in agenda", "aggiungi in calendario", "segna in agenda"]
RECAP_KWS = ["recap agenda", "cosa ho in agenda", "mostra agenda", "agenda", "calendario"]
REMOVE_KWS = ["rimuovi", "cancella", "elimina", "togli"]
MOVE_KWS = ["sposta", "ripianifica", "posticipa", "anticipa", "rimanda", "porta a"]
HELP_KWS = ["/help", "aiuto"]

def detect_intent(text: str) -> str:
    """Decide l'intento in base a frasi chiave; se trova una data senza trigger espliciti, assume 'add'."""
    t = text.lower()
    if any(k in t for k in RECAP_KWS):
        return INTENT_RECAP
    if any(k in t for k in MOVE_KWS):
        return INTENT_MOVE
    if any(k in t for k in REMOVE_KWS):
        return INTENT_REMOVE
    if any(k in t for k in ADD_KWS):
        return INTENT_ADD
    if any(k in t for k in HELP_KWS):
        return INTENT_HELP
    # fallback: se c'è una data/ora riconoscibile, probabilmente è un'aggiunta
    if search_dates(t, languages=['it']):
        return INTENT_ADD
    return INTENT_UNKNOWN

def extract_datetime(text: str, now_dt: datetime) -> Optional[datetime]:
    """
    Estrae la prima data/ora plausibile dal testo in italiano.
    - Preferisce date future
    - Se non c'è l'ora, imposta 09:00
    - Ritorna timezone Europe/Rome
    """
    results = search_dates(text, languages=['it'], settings={'PREFER_DATES_FROM': 'future'})
    if not results:
        return None
    _, dt = results[0]
    # Normalizza timezone
    if dt.tzinfo is None:
        dt = ROME_TZ.localize(dt)
    else:
        dt = dt.astimezone(ROME_TZ)
    # Se non hai specificato l'ora nel testo, metti 09:00
    has_time = bool(re.search(r"\b\d{1,2}(:\d{2})\b", text))
    if dt.hour == 0 and dt.minute == 0 and not has_time:
        dt = dt.replace(hour=9, minute=0, second=0, microsecond=0)
    return dt

def strip_date_from_title(text: str) -> str:
    """
    Ripulisce il testo da parole di calendario e pattern di date/ore, lasciando il titolo.
    Esempio: 'metti in agenda domani alle 15 riunione budget' -> 'Riunione Budget'
    """
    t = text.lower()
    for kw in ADD_KWS + ["metti", "aggiungi", "agenda", "calendario", "metti in", "inserisci", "segna"]:
        t = t.replace(kw, " ")
    t = re.sub(r"\b(oggi|domani|dopodomani|stamattina|stasera|stanotte|lunedì|martedì|mercoledì|giovedì|venerdì|sabato|domenica)\b", " ", t)
    t = re.sub(r"\b(\d{1,2}(:\d{2})?)\b", " ", t)
    t = re.sub(r"\d{1,2}/\d{1,2}(/\d{2,4})?", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.title() if t else ""

def extract_move_targets(text: str) -> Tuple[Optional[str], Optional[datetime]]:
    """
    Cerca 'titolo' e 'nuova data' da frasi tipo:
    'sposta riunione budget a lunedì alle 10'
    """
    lower = text.lower()
    parts = re.split(r"\b a | per | al ", lower, maxsplit=1)
    event_part = parts[0]
    for kw in MOVE_KWS:
        event_part = event_part.replace(kw, " ")
    event_part = re.sub(r"\s+", " ", event_part).strip()
    title_guess = event_part.title() if event_part else None
    new_dt = extract_datetime(text, now_dt=datetime.now(ROME_TZ))
    return title_guess, new_dt

def extract_remove_target(text: str) -> Tuple[Optional[str], Optional[datetime]]:
    """
    Prova a capire cosa rimuovere, accettando sia titolo che un orario specifico.
    Esempi: 'rimuovi visita commercialista', 'cancella evento di domani alle 15'
    """
    lower = text.lower()
    for kw in REMOVE_KWS:
        lower = lower.replace(kw, " ")
    lower = re.sub(r"\s+", " ", lower).strip()
    dt = extract_datetime(lower, now_dt=datetime.now(ROME_TZ))
    title_guess = strip_date_from_title(lower).title()
    if title_guess == "":
        title_guess = None
    return title_guess, dt
