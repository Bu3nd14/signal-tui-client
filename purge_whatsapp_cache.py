#!/usr/bin/env python3
"""Purge one-shot dei messaggi WhatsApp dal DB locale (senza toccare Signal).

Elimina SOLO le righe ``WHERE protocol = 'whatsapp'`` dalla tabella ``messages``
di ``~/.local/share/signal-tui-client/messages.db``.  Le chat Signal non vengono
toccate.  Lo storico WhatsApp viene poi ricostruito automaticamente dalla TUI al
prossimo avvio (vedi ``WhatsAppBackend.resync_history``, che scarica da WAHA
l'unione di unread + chat con messaggi nel DB).

Fail-safe: prima di cancellare verifica che WAHA sia raggiungibile (via
``get_session_status``).  Se il servizio non risponde lo script ESCE e non
cancella nulla, altrimenti un successivo avvio della TUI partirebbe con un DB
svuotato senza poterlo riscaricare subito.

Prima di toccare il DB crea una copia di backup ``messages.db.bak-<timestamp>``.

Usage:
    python3 purge_whatsapp_cache.py
"""

import logging
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from models import PROTOCOL_WHATSAPP
from protocols.config import resolve_whatsapp_api_url
from protocols.whatsapp import WhatsAppRESTClient

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".local" / "share" / "signal-tui-client"
DB_FILE = CACHE_DIR / "messages.db"

# Si riusa il lock del backend per non corrompere un DB in uso dalla TUI.
from protocols.db import _DB_LOCK

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol TEXT NOT NULL DEFAULT 'signal',
    contact_number TEXT NOT NULL,
    text TEXT,
    is_mine INTEGER NOT NULL DEFAULT 0,
    sender TEXT,
    timestamp INTEGER NOT NULL,
    quote_text TEXT,
    msg_type TEXT DEFAULT 'text',
    attachment_info TEXT,
    attachment_id TEXT,
    read INTEGER DEFAULT 0,
    status TEXT DEFAULT 'read'
)
"""


def _count_rows(conn: sqlite3.Connection, protocol: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE protocol = ?", (protocol,)
    ).fetchone()
    return int(row[0]) if row else 0


def _whatsapp_online() -> bool:
    """True se WAHA è raggiungibile (get_session_status non è None)."""
    try:
        client = WhatsAppRESTClient(resolve_whatsapp_api_url())
        status = client.get_session_status()
        return status is not None
    except Exception as _e:
        logger.debug("WAHA reachability check failed", exc_info=True)
        return False


def purge(db_file: Path | None = None) -> int:
    """Rimuove i messaggi WhatsApp dal DB. Ritorna il numero di righe eliminate.

    Non fa nulla se WAHA non è raggiungibile (fail-safe).  ``db_file`` è
    override per i test; di default usa il DB reale.
    """
    db_file = db_file or DB_FILE
    if not db_file.exists():
        print("No cache DB found, nothing to purge.")
        return 0

    if not _whatsapp_online():
        print(
            "ABORT: WAHA non è raggiungibile. Purge annullato (fail-safe): "
            "riprova quando il servizio è online, altrimenti la TUI "
            "partirebbe con un DB svuotato senza poterlo riscaricare."
        )
        sys.exit(1)

    # Backup prima di toccare qualsiasi cosa.
    backup = db_file.with_name(f"messages.db.bak-{int(time.time())}")
    shutil.copy2(db_file, backup)
    print(f"Backup creato: {backup}")

    with _DB_LOCK:
        conn = sqlite3.connect(db_file)
        try:
            conn.execute(SCHEMA)
            n = _count_rows(conn, PROTOCOL_WHATSAPP)
            if n == 0:
                print("Nessun messaggio WhatsApp da rimuovere.")
                conn.commit()
                return 0
            conn.execute(
                "DELETE FROM messages WHERE protocol = ?", (PROTOCOL_WHATSAPP,)
            )
            conn.commit()  # commit prima del VACUUM
            conn.execute("VACUUM")
            conn.commit()
            print(f"Rimossi {n} messaggi WhatsApp (Signal invariato).")
            return n
        finally:
            conn.close()

    return 0


if __name__ == "__main__":
    removed = purge()
    if removed:
        print(
            "Fatto. Riavvia la TUI: scaricherà di nuovo lo storico WhatsApp "
            "dalle chat unread + quelle con messaggi nel DB."
        )
