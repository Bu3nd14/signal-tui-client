"""
Regression test for docker-compose.yml

WAHA (WhatsApp HTTP API) delivers incoming messages to the Python client by
posting webhooks to ``http://host.docker.internal:8088/webhook``.  On Linux the
container cannot resolve the ``host.docker.internal`` alias unless it is
mapped to the host gateway explicitly, so ``docker-compose.yml`` must keep the
``extra_hosts`` entry ``host.docker.internal:host-gateway`` on the ``whatsapp``
service.  If it is ever dropped, the webhook (and therefore real-time message
updates for non-selected WhatsApp contacts) silently stops working.

The test reads the file as plain text (no yaml dependency is needed by the
project) and asserts the expected snippet is present inside the ``whatsapp``
service block.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"

# Mapping expected by WAHA to reach the host's webhook server on Linux.
HOST_GATEWAY = "host.docker.internal:host-gateway"
FILES_LIFETIME = 'WHATSAPP_FILES_LIFETIME: "0"'


def _service_block(text: str, service: str) -> str:
    """Return the YAML block for *service* (up to the next top-level key)."""
    match = re.search(rf"\n  {re.escape(service)}:\n", text)
    assert match, f"service {service!r} not found in docker-compose.yml"
    start = match.end()
    # Next line at column 0 (top-level key) ends the service block.
    rest = text[start:]
    next_toplevel = re.search(r"\n[A-Za-z0-9_].*:\n", rest)
    end = next_toplevel.start() if next_toplevel else len(rest)
    block = rest[:end]
    assert block.strip(), f"service {service!r} block is empty"
    return block


def test_compose_file_exists() -> None:
    assert COMPOSE_FILE.is_file(), f"missing {COMPOSE_FILE}"


def test_whatsapp_service_extra_hosts_host_gateway() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    block = _service_block(text, "whatsapp")

    # 1) The ``extra_hosts`` key must be present inside the service block.
    extra_hosts_match = re.search(r"^    extra_hosts:\s*(?:#.*)?$", block, re.MULTILINE)
    assert extra_hosts_match, (
        "docker-compose.yml: the 'whatsapp' service is missing 'extra_hosts'. "
        "Keep 'host.docker.internal:host-gateway' so WAHA can reach the host "
        "webhook on Linux."
    )

    # 2) The host-gateway mapping must list the host.docker.internal alias.
    assert HOST_GATEWAY in block, (
        "docker-compose.yml: 'extra_hosts' must contain "
        f"'{HOST_GATEWAY}' for WAHA webhook delivery on Linux."
    )

    # 3) Sanity: the webhook URL must still target host.docker.internal, so the
    #    alias and the target stay consistent.
    assert "host.docker.internal" in block


def test_whatsapp_media_files_do_not_expire() -> None:
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    block = _service_block(text, "whatsapp")

    assert FILES_LIFETIME in block
