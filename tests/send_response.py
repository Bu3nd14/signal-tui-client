"""Helper per i test di contratto di ``messages.send`` / ``POST /api/send``.

La risposta di un invio riuscito porta ``message_ids`` (id reali creati dal
backend, usati dal client web per accoppiare la bolla optimistic).  I test
che verificano altro (idempotenza, retry, upload) confrontano la risposta
senza quel campo; ``test_send_message_ids.py`` ne verifica il valore.
"""

from __future__ import annotations

from typing import Any


def strip_ids(body: Any) -> Any:
    """Copia di *body* senza ``message_ids`` (invariato se non è un dict)."""
    if not isinstance(body, dict):
        return body
    return {key: value for key, value in body.items() if key != "message_ids"}
