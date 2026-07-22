#!/usr/bin/env python3
"""Offline migration script: add ``status`` field to old cached messages.

Messages created before the read-receipt feature was implemented do not
have a ``status`` field.  This script adds ``"status": "read"`` to every
sent message (``is_mine=True``) that lacks the field, so they render
normally instead of in *italic* (which is reserved for messages awaiting
a delivery receipt).

Run this script while the Signal TUI client is **closed**::

    python3 migrate_cache_status.py

It reads/writes the cache file at ``~/.local/share/signal-tui-client/messages.json``.
"""

import json
from pathlib import Path

CACHE_DIR = Path.home() / ".local" / "share" / "signal-tui-client"
CACHE_FILE = CACHE_DIR / "messages.json"


def main() -> None:
    if not CACHE_FILE.exists():
        print(f"❌ Cache file not found: {CACHE_FILE}")
        print("   Nothing to migrate.")
        return

    with open(CACHE_FILE, "r") as f:
        cache = json.load(f)

    total_migrated = 0

    for contact, messages in cache.items():
        for msg in messages:
            # Only touch sent messages that lack the status field
            if "status" not in msg and msg.get("is_mine", False):
                msg["status"] = "read"
                total_migrated += 1

    if total_migrated == 0:
        print("✅ No messages needed migration (all already have a status field).")
        return

    # Write back
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

    print(f"✅ Migrated {total_migrated} message(s): added \"status\": \"read\".")
    print("   You can now start the Signal TUI client.")


if __name__ == "__main__":
    main()
