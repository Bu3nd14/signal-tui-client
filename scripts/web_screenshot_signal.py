"""Cattura la reaction Signal nella web UI pilotando Chrome tramite CDP."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import web_screenshot as base

PROTO = "signal"
TARGET_CONTACT_ID = "+4915254804614"
TARGET_MESSAGE_ID = "1787950650355"
CONTACTS_SCREENSHOT = Path("/tmp/web-signal-contacts.png")
CHAT_SCREENSHOT = Path("/tmp/web-signal-chat.png")
REACTION_SCREENSHOT = Path("/tmp/web-signal-reaction.png")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cattura lista contatti, chat e reaction Signal tramite CDP."
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


async def select_target_contact(cdp: base.CDPClient) -> dict[str, Any]:
    target_literal = json.dumps(TARGET_CONTACT_ID)
    proto_literal = json.dumps(PROTO)
    expression = f"""
(() => {{
  const targetId = {target_literal};
  const protocol = {proto_literal};
  const root = document.querySelector('#contact-list');
  const buttons = [...(root?.querySelectorAll(
    'button.contact, .contact-item, li, [data-contact]'
  ) || [])];
  let element = buttons.find((item) =>
    Object.values(item.dataset || {{}}).some((value) => String(value).includes(targetId))
    || (item.textContent || '').includes(targetId)
  );
  let strategy = 'DOM id/testo';

  if (!element && typeof state !== 'undefined' && Array.isArray(state.contacts)) {{
    const contacts = [...state.contacts]
      .sort((a, b) => Number(b.last_message_ts || 0) - Number(a.last_message_ts || 0))
      .filter((contact) => contact.protocol === protocol);
    const index = contacts.findIndex((contact) => String(contact.id) === targetId);
    if (index >= 0) {{
      element = buttons[index];
      strategy = 'indice da state.contacts Signal';
    }}
  }}

  if (!element) return {{clicked: false, strategy: 'target Signal non trovato'}};
  element.click();
  return {{
    clicked: true,
    strategy,
    label: (element.innerText || element.textContent || '').trim().slice(0, 200)
  }};
}})()
"""
    result = await cdp.evaluate(expression)
    return result if isinstance(result, dict) else {"clicked": False}


async def select_first_visible_signal(cdp: base.CDPClient) -> dict[str, Any]:
    expression = """
(() => {
  const root = document.querySelector('#contact-list');
  const buttons = [...(root?.querySelectorAll(
    'button.contact, .contact-item, li, [data-contact]'
  ) || [])];
  const signalContacts = typeof state !== 'undefined' && Array.isArray(state.contacts)
    ? [...state.contacts]
        .sort((a, b) => Number(b.last_message_ts || 0) - Number(a.last_message_ts || 0))
        .filter((contact) => contact.protocol === 'signal')
    : [];
  const index = signalContacts.findIndex((contact) => contact.protocol === 'signal');
  let element = index >= 0 ? buttons[index] : null;
  if (!element) {
    element = buttons.find((item) => {
      const style = getComputedStyle(item);
      return item.getClientRects().length > 0
        && style.display !== 'none'
        && style.visibility !== 'hidden';
    });
  }
  if (!element) return {clicked: false, strategy: 'nessun elemento Signal visibile'};
  element.click();
  return {
    clicked: true,
    strategy: 'prima chat Signal visibile',
    label: (element.innerText || element.textContent || '').trim().slice(0, 200)
  };
})()
"""
    result = await cdp.evaluate(expression)
    return result if isinstance(result, dict) else {"clicked": False}


async def drive_browser(cdp: base.CDPClient, token: str) -> list[str]:
    summary: list[str] = []
    for domain in ("Page.enable", "Runtime.enable"):
        await cdp.send_command(domain)
    summary.append("domini CDP abilitati")

    app_url_literal = json.dumps(base.APP_URL)
    current_url = await base.wait_for_condition(
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
    summary.append(f"pagina caricata sull'origin corretto: {current_url}")

    token_literal = json.dumps(token)
    await cdp.evaluate(
        "localStorage.setItem('signal-tui-web-token', "
        f"{token_literal}); "
        "localStorage.setItem('signal-tui-web-proto', 'signal'); true"
    )
    stored_values = await cdp.evaluate(
        "({token: localStorage.getItem('signal-tui-web-token'), "
        "proto: localStorage.getItem('signal-tui-web-proto')})"
    )
    storage_verified = stored_values == {"token": token, "proto": PROTO}
    print(
        f"Verifica localStorage token/protocollo: {'OK' if storage_verified else 'FALLITA'}"
    )
    if not storage_verified:
        raise RuntimeError("verifica di token e protocollo in localStorage fallita")
    await base.reload_and_wait(cdp)
    summary.append("token verificato, protocollo Signal impostato e pagina ricaricata")

    contact_count_expression = """
(() => document.querySelectorAll(
  '#contact-list .contact, #contact-list .contact-item, #contact-list li, #contact-list [data-contact]'
).length)()
"""
    contact_count = await base.wait_for_condition(
        cdp, contact_count_expression, "il caricamento dei contatti Signal"
    )
    summary.append(f"contatti Signal caricati: {contact_count}")

    width, height, size = await base.capture_screenshot(cdp, CONTACTS_SCREENSHOT)
    summary.append(f"{CONTACTS_SCREENSHOT}: {width}x{height}, {size} byte")

    click_result = await select_target_contact(cdp)
    if not click_result.get("clicked"):
        print("Target Signal non selezionato; diagnostica prima del fallback:")
        await base.print_diagnostics(cdp)
        click_result = await select_first_visible_signal(cdp)
    if not click_result.get("clicked"):
        raise RuntimeError("nessuna chat Signal cliccabile trovata")
    summary.append(
        f"chat aperta con strategia: {click_result['strategy']} "
        f"({click_result.get('label', '')})"
    )

    await base.wait_for_condition(
        cdp,
        "document.querySelectorAll('#message-list .message').length",
        "il caricamento dei messaggi Signal",
        timeout=30.0,
    )
    target_resolution = await cdp.evaluate(
        "(() => { const id = '1787950650355'; "
        'const byId = document.querySelector(`[data-mid="${id}"]`); '
        "if (byId) return {matched:'data-mid', originalMid:byId.dataset.mid}; "
        'const byTimestamp = document.querySelector(`[data-ts="${id}"]`); '
        "if (!byTimestamp) return {matched:null}; "
        "const originalMid = byTimestamp.dataset.mid; "
        "byTimestamp.dataset.mid = id; "
        "return {matched:'data-ts', originalMid}; })()"
    )
    summary.append(
        "messaggio target risolto: " + json.dumps(target_resolution, ensure_ascii=False)
    )
    reaction_selector = json.dumps(f'[data-mid="{TARGET_MESSAGE_ID}"] .reaction-chip')
    await base.wait_for_condition(
        cdp,
        f"Boolean(document.querySelector({reaction_selector}))",
        "la reaction-chip sul messaggio target",
        timeout=30.0,
        interval=0.25,
    )
    reaction_result = await cdp.evaluate(
        "(() => { const el = document.querySelector('[data-mid=\"1787950650355\"]'); "
        "if (!el) return {found:false}; el.scrollIntoView({block:'center'}); "
        "const chip = el.querySelector('.reaction-chip'); "
        "return {found:true, chip: chip ? chip.textContent.trim() : null, "
        "aria: el.querySelector('.message-reactions')?.getAttribute('aria-label') || null}; })()"
    )
    print(
        "Verifica reaction-chip:",
        json.dumps(reaction_result, ensure_ascii=False),
    )
    summary.append(
        "reaction-chip verificata: " + json.dumps(reaction_result, ensure_ascii=False)
    )
    await asyncio.sleep(1.0)

    width, height, size = await base.capture_screenshot(cdp, CHAT_SCREENSHOT)
    summary.append(f"{CHAT_SCREENSHOT}: {width}x{height}, {size} byte")

    await cdp.send_command(
        "Emulation.setDeviceMetricsOverride",
        {
            "width": 1400,
            "height": 900,
            "deviceScaleFactor": 2,
            "mobile": False,
        },
    )
    await cdp.evaluate(
        "document.querySelector('[data-mid=\"1787950650355\"]')"
        "?.scrollIntoView({block:'center'}); true"
    )
    await asyncio.sleep(1.0)
    width, height, size = await base.capture_screenshot(cdp, REACTION_SCREENSHOT)
    summary.append(f"{REACTION_SCREENSHOT}: {width}x{height}, {size} byte")
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
