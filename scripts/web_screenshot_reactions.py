"""Cattura le reaction Signal, WhatsApp e Telegram nella web UI tramite CDP."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import web_screenshot as base
from PIL import Image


@dataclass(frozen=True)
class ShotCase:
    proto: str
    contact_id: str
    data_ts: str
    emoji: str
    data_mid: str | None = None

    @property
    def chat_path(self) -> Path:
        return Path(f"/tmp/web-chat-{self.proto}.png")

    @property
    def reaction_path(self) -> Path:
        return Path(f"/tmp/web-reaction-{self.proto}.png")


CASES = (
    ShotCase("signal", "+4915254804614", "1787950650355", "❤️"),
    ShotCase(
        "whatsapp",
        "189025889575055@lid",
        "1787950660000",
        "👍",
        "false_189025889575055@lid_3A626A122CEF68046FBD",
    ),
    ShotCase("telegram", "8829363612", "1787950637000", "❤️", "1403"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cattura le reaction delle chat di test tramite Chrome CDP."
    )
    parser.add_argument(
        "token",
        nargs="?",
        help="Bearer token; se omesso viene letto da config.json",
    )
    return parser.parse_args()


def find_chrome() -> str:
    stable = Path("/usr/bin/google-chrome-stable")
    if stable.is_file():
        return str(stable)
    for executable in ("google-chrome-stable", "google-chrome", "chromium"):
        if path := shutil.which(executable):
            return path
    raise RuntimeError("Google Chrome o Chromium non trovato")


async def select_contact(cdp: base.CDPClient, case: ShotCase) -> dict[str, Any]:
    target_literal = json.dumps(case.contact_id)
    proto_literal = json.dumps(case.proto)
    expression = f"""
(() => {{
  const targetId = {target_literal};
  const protocol = {proto_literal};
  const root = document.querySelector('#contact-list');
  const buttons = [...(root?.querySelectorAll('button.contact') || [])];
  const contacts = typeof state !== 'undefined' && Array.isArray(state.contacts)
    ? [...state.contacts]
        .filter((contact) => contact.protocol === protocol)
        .sort((a, b) => Number(b.last_message_ts || 0) - Number(a.last_message_ts || 0))
    : [];
  const contactIndex = contacts.findIndex((contact) => String(contact.id) === targetId);
  let element = contactIndex >= 0 ? buttons[contactIndex] : null;
  let strategy = 'indice da state.contacts filtrato per protocollo';

  if (!element) {{
    element = buttons.find((item) =>
      Object.values(item.dataset || {{}}).some((value) => String(value).includes(targetId))
      || (item.textContent || '').includes(targetId)
    );
    strategy = 'dataset/testo DOM';
  }}
  if (!element) {{
    return {{
      clicked: false,
      contactFound: contactIndex >= 0,
      contactCount: contacts.length,
      buttonCount: buttons.length
    }};
  }}
  element.click();
  return {{
    clicked: true,
    strategy,
    contactIndex,
    label: (element.innerText || element.textContent || '').trim().slice(0, 200)
  }};
}})()
"""
    result = await cdp.evaluate(expression)
    return result if isinstance(result, dict) else {"clicked": False}


def target_expression(case: ShotCase, body: str) -> str:
    ts_literal = json.dumps(case.data_ts)
    mid_literal = json.dumps(case.data_mid) if case.data_mid else "null"
    return f"""
(() => {{
  const target = document.querySelector(`[data-ts=${{JSON.stringify({ts_literal})}}]`)
    || ({mid_literal} && document.querySelector(`[data-mid=${{JSON.stringify({mid_literal})}}]`));
  {body}
}})()
"""


async def print_case_diagnostics(cdp: base.CDPClient, case: ShotCase) -> None:
    await base.print_diagnostics(cdp)
    expression = target_expression(
        case,
        """
  return {
    targetFound: Boolean(target),
    targetHtml: target?.outerHTML.slice(0, 1200) || '',
    reactionChips: [...document.querySelectorAll('.reaction-chip')].map((chip) => ({
      text: chip.textContent.trim(),
      title: chip.getAttribute('title') || '',
      ts: chip.closest('[data-ts]')?.dataset.ts || '',
      mid: chip.closest('[data-mid]')?.dataset.mid || ''
    }))
  };
""",
    )
    try:
        details = await cdp.evaluate(expression, timeout=3.0)
        print(
            f"Diagnostica reaction {case.proto}:",
            json.dumps(details, ensure_ascii=False),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Diagnostica reaction {case.proto} non disponibile: {exc}")


def verify_png(path: Path) -> tuple[int, int, int]:
    size = path.stat().st_size
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        width, height = image.size
        if image.format != "PNG":
            raise RuntimeError(f"{path} non è un PNG")
    if size <= 20 * 1024:
        raise RuntimeError(f"{path} è troppo piccolo: {size} byte")
    return width, height, size


async def capture_case(
    cdp: base.CDPClient, token: str, case: ShotCase
) -> dict[str, Any]:
    await cdp.send_command(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1400, "height": 900, "deviceScaleFactor": 1, "mobile": False},
    )
    token_literal = json.dumps(token)
    proto_literal = json.dumps(case.proto)
    await cdp.evaluate(
        f"localStorage.setItem('signal-tui-web-token', {token_literal});"
        f"localStorage.setItem('signal-tui-web-proto', {proto_literal}); true"
    )
    stored = await cdp.evaluate(
        "({token: localStorage.getItem('signal-tui-web-token'), "
        "proto: localStorage.getItem('signal-tui-web-proto')})"
    )
    if stored != {"token": token, "proto": case.proto}:
        raise RuntimeError(f"verifica localStorage fallita: {stored!r}")
    await base.reload_and_wait(cdp)

    await base.wait_for_condition(
        cdp,
        "document.querySelectorAll('#contact-list button.contact').length",
        f"i contatti {case.proto}",
    )
    click_result = await select_contact(cdp, case)
    if not click_result.get("clicked"):
        raise RuntimeError(
            "contatto target non selezionato: "
            + json.dumps(click_result, ensure_ascii=False)
        )
    print(
        f"{case.proto}: chat aperta:",
        json.dumps(click_result, ensure_ascii=False),
    )

    await base.wait_for_condition(
        cdp,
        "document.querySelectorAll('#message-list .message').length",
        f"i messaggi {case.proto}",
        timeout=30.0,
        interval=0.25,
    )
    emoji_literal = json.dumps(case.emoji, ensure_ascii=False)
    reaction_condition = target_expression(
        case,
        f"""
  if (!target) return false;
  target.scrollIntoView({{block: 'center'}});
  return [...target.querySelectorAll('.reaction-chip')]
    .some((chip) => chip.textContent.trim().includes({emoji_literal}));
""",
    )
    await base.wait_for_condition(
        cdp,
        reaction_condition,
        f"la reaction-chip {case.emoji} sul messaggio target {case.proto}",
        timeout=30.0,
        interval=0.25,
    )
    reaction_result = await cdp.evaluate(
        target_expression(
            case,
            f"""
  if (!target) return {{found: false}};
  target.scrollIntoView({{block: 'center'}});
  const chip = [...target.querySelectorAll('.reaction-chip')]
    .find((item) => item.textContent.trim().includes({emoji_literal}));
  return {{
    found: Boolean(chip),
    text: chip?.textContent.trim() || null,
    ariaLabel: target.querySelector('.message-reactions')?.getAttribute('aria-label') || null
  }};
""",
        )
    )
    await asyncio.sleep(1.0)

    await base.capture_screenshot(cdp, case.chat_path)
    chat_dimensions = verify_png(case.chat_path)

    await cdp.send_command(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1400, "height": 900, "deviceScaleFactor": 2, "mobile": False},
    )
    await cdp.evaluate(
        target_expression(
            case,
            "target?.scrollIntoView({block: 'center'}); return Boolean(target);",
        )
    )
    await asyncio.sleep(1.0)
    await base.capture_screenshot(cdp, case.reaction_path)
    reaction_dimensions = verify_png(case.reaction_path)

    return {
        "ok": True,
        "chip": reaction_result,
        "chat": chat_dimensions,
        "reaction": reaction_dimensions,
    }


async def drive_browser(cdp: base.CDPClient, token: str) -> list[str]:
    for domain in ("Page.enable", "Runtime.enable"):
        await cdp.send_command(domain)
    app_url_literal = json.dumps(base.APP_URL)
    await base.wait_for_condition(
        cdp,
        f"location.href.startsWith({app_url_literal}) ? location.href : ''",
        "l'origin della web UI",
        timeout=30.0,
        interval=0.25,
    )
    await base.wait_for_condition(
        cdp,
        "document.readyState === 'complete'",
        "il caricamento completo della web UI",
        timeout=30.0,
        interval=0.25,
    )

    results: dict[str, dict[str, Any]] = {}
    for case in CASES:
        print(f"\n=== {case.proto.upper()} ===")
        try:
            results[case.proto] = await capture_case(cdp, token, case)
        except Exception as exc:  # noqa: BLE001
            results[case.proto] = {"ok": False, "error": str(exc)}
            print(f"ERRORE {case.proto}: {exc}", file=sys.stderr)
            await print_case_diagnostics(cdp, case)

    print("\nRISULTATI FINALI:")
    summary: list[str] = []
    for case in CASES:
        result = results[case.proto]
        if not result["ok"]:
            line = f"{case.proto}: FALLITA - {result['error']}"
        else:
            chip = result["chip"]
            chat_width, chat_height, chat_size = result["chat"]
            reaction_width, reaction_height, reaction_size = result["reaction"]
            line = (
                f"{case.proto}: OK - chip={chip.get('text')!r}, "
                f"aria-label={chip.get('ariaLabel')!r}; "
                f"{case.chat_path}={chat_width}x{chat_height}, {chat_size} byte; "
                f"{case.reaction_path}={reaction_width}x{reaction_height}, "
                f"{reaction_size} byte"
            )
        print(line)
        summary.append(line)
    return summary


def main() -> int:
    args = parse_args()
    try:
        token = base.load_token(args.token)
        chrome_path = find_chrome()
    except RuntimeError as exc:
        print(f"ERRORE: {exc}", file=sys.stderr)
        return 1
    base.drive_browser = drive_browser
    return asyncio.run(base.async_main(token, chrome_path))


if __name__ == "__main__":
    raise SystemExit(main())
