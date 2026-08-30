"""Cattura schermate della web UI pilotando Chrome tramite CDP."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import struct
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

import websockets

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
PROFILE_PATH = Path("/tmp/web-shot-profile")
CONTACTS_SCREENSHOT = Path("/tmp/web-contacts.png")
CHAT_SCREENSHOT = Path("/tmp/web-chat.png")
DEBUG_URL = "http://127.0.0.1:9222/json"
APP_URL = "http://127.0.0.1:4242/"
TARGET_CONTACT_ID = "16660245291231@lid"


class CDPError(RuntimeError):
    """Errore restituito da Chrome DevTools Protocol."""


class CDPClient:
    def __init__(self, websocket: Any) -> None:
        self.websocket = websocket
        self.next_id = 1
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.events: defaultdict[str, asyncio.Queue[dict[str, Any]]] = defaultdict(
            asyncio.Queue
        )
        self.listener: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.listener = asyncio.create_task(self._listen(), name="cdp-listener")

    async def _listen(self) -> None:
        failure: BaseException | None = None
        try:
            async for raw_message in self.websocket:
                message = json.loads(raw_message)
                command_id = message.get("id")
                if command_id is not None:
                    future = self.pending.pop(command_id, None)
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message["error"]
                        future.set_exception(
                            CDPError(
                                f"CDP {error.get('code', '?')}: "
                                f"{error.get('message', 'errore sconosciuto')}"
                            )
                        )
                    else:
                        future.set_result(message.get("result", {}))
                elif method := message.get("method"):
                    self.events[method].put_nowait(message.get("params", {}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            failure = exc
        finally:
            error = failure or ConnectionError("connessione CDP chiusa")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()

    async def send_command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        command_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[command_id] = future
        payload: dict[str, Any] = {"id": command_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self.websocket.send(json.dumps(payload))
            return await asyncio.wait_for(future, timeout)
        except BaseException:
            self.pending.pop(command_id, None)
            if not future.done():
                future.cancel()
            raise

    def clear_events(self, method: str) -> None:
        queue = self.events[method]
        while not queue.empty():
            queue.get_nowait()

    async def wait_event(self, method: str, timeout: float = 15.0) -> dict[str, Any]:
        return await asyncio.wait_for(self.events[method].get(), timeout)

    async def evaluate(self, expression: str, timeout: float = 15.0) -> Any:
        response = await self.send_command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
            timeout,
        )
        if details := response.get("exceptionDetails"):
            description = (
                details.get("exception", {}).get("description")
                or details.get("text")
                or "eccezione JavaScript"
            )
            raise CDPError(description)
        return response.get("result", {}).get("value")

    async def close(self) -> None:
        if self.listener is not None and not self.listener.done():
            self.listener.cancel()
            await asyncio.gather(self.listener, return_exceptions=True)
        await self.websocket.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cattura lista contatti e chat dalla web UI tramite CDP."
    )
    parser.add_argument(
        "token",
        nargs="?",
        help="Bearer token; se omesso viene letto da config.json",
    )
    return parser.parse_args()


def load_token(cli_token: str | None) -> str:
    if cli_token:
        return cli_token
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        token = config["web"]["token"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"impossibile leggere web.token da {CONFIG_PATH}") from exc
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"web.token non valido in {CONFIG_PATH}")
    return token


def find_chrome() -> str:
    for executable in ("chromium", "google-chrome"):
        if path := shutil.which(executable):
            return path
    raise RuntimeError("chromium o google-chrome non trovato nel PATH")


def fetch_page_websocket_url() -> str:
    with urllib.request.urlopen(DEBUG_URL, timeout=1.0) as response:
        targets = json.load(response)
    pages = [
        target
        for target in targets
        if target.get("type") == "page" and target.get("webSocketDebuggerUrl")
    ]
    for target in pages:
        if target.get("url") != "about:blank":
            return str(target["webSocketDebuggerUrl"])
    if pages:
        return str(pages[0]["webSocketDebuggerUrl"])
    raise RuntimeError("nessun target CDP di tipo page disponibile")


async def wait_for_debugger(process: asyncio.subprocess.Process) -> str:
    deadline = asyncio.get_running_loop().time() + 15.0
    last_error: BaseException | None = None
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            raise RuntimeError(f"Chrome terminato con codice {process.returncode}")
        try:
            return await asyncio.to_thread(fetch_page_websocket_url)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await asyncio.sleep(0.25)
    raise TimeoutError(f"debugger CDP non disponibile: {last_error}")


async def wait_for_condition(
    cdp: CDPClient,
    expression: str,
    description: str,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    last_value: Any = None
    while asyncio.get_running_loop().time() < deadline:
        last_value = await cdp.evaluate(expression)
        if last_value:
            return last_value
        await asyncio.sleep(interval)
    raise TimeoutError(
        f"timeout attendendo {description}; ultimo valore: {last_value!r}"
    )


async def reload_and_wait(cdp: CDPClient) -> None:
    cdp.clear_events("Page.loadEventFired")
    await cdp.send_command("Page.reload", {"ignoreCache": False})
    await cdp.wait_event("Page.loadEventFired", timeout=20.0)


async def capture_screenshot(cdp: CDPClient, destination: Path) -> tuple[int, int, int]:
    response = await cdp.send_command(
        "Page.captureScreenshot", {"format": "png", "fromSurface": True}, timeout=30.0
    )
    data = base64.b64decode(response["data"], validate=True)
    destination.write_bytes(data)
    if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
        raise RuntimeError("Chrome ha restituito uno screenshot PNG non valido")
    width, height = struct.unpack(">II", data[16:24])
    return width, height, len(data)


async def print_diagnostics(cdp: CDPClient) -> None:
    expression = """
(() => ({
  url: location.href,
  readyState: document.readyState,
  contactStatus: document.querySelector('#contact-status')?.innerText || '',
  contacts: document.querySelector('#contact-list')?.innerText.slice(0, 500) || '',
  thread: document.querySelector('#thread-name')?.innerText || '',
  messages: document.querySelector('#message-list')?.innerText.slice(0, 1000) || '',
  error: document.querySelector('#error-banner:not([hidden])')?.innerText || ''
}))()
"""
    try:
        diagnostics = await cdp.evaluate(expression, timeout=3.0)
        print("Diagnostica pagina:", json.dumps(diagnostics, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        print(f"Diagnostica pagina non disponibile: {exc}")


async def drive_browser(cdp: CDPClient, token: str) -> list[str]:
    summary: list[str] = []
    for domain in ("Page.enable", "Runtime.enable"):
        await cdp.send_command(domain)
    summary.append("domini CDP abilitati")

    app_url_literal = json.dumps(APP_URL)
    current_url = await wait_for_condition(
        cdp,
        f"location.href.startsWith({app_url_literal}) ? location.href : ''",
        "l'origin della web UI",
        timeout=30.0,
        interval=0.25,
    )
    await wait_for_condition(
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
        "localStorage.setItem('signal-tui-web-proto', 'whatsapp'); true"
    )
    stored_token = await cdp.evaluate("localStorage.getItem('signal-tui-web-token')")
    token_verified = stored_token == token
    print(f"Verifica localStorage token: {'OK' if token_verified else 'FALLITA'}")
    if not token_verified:
        raise RuntimeError("verifica del token in localStorage fallita")
    await reload_and_wait(cdp)
    summary.append("token verificato, protocollo impostato e pagina ricaricata")

    contact_count_expression = """
(() => document.querySelectorAll(
  '#contact-list .contact, #contact-list .contact-item, #contact-list li, #contact-list [data-contact]'
).length)()
"""
    contact_count = await wait_for_condition(
        cdp, contact_count_expression, "il caricamento dei contatti"
    )
    summary.append(f"contatti caricati: {contact_count}")

    width, height, size = await capture_screenshot(cdp, CONTACTS_SCREENSHOT)
    summary.append(f"{CONTACTS_SCREENSHOT}: {width}x{height}, {size} byte")

    target_literal = json.dumps(TARGET_CONTACT_ID)
    click_expression = f"""
(() => {{
  const targetId = {target_literal};
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
      .filter((contact) => contact.protocol === 'whatsapp');
    const index = contacts.findIndex((contact) => String(contact.id) === targetId);
    if (index >= 0) {{
      element = buttons[index];
      strategy = 'indice da state.contacts';
    }}
  }}

  if (!element) {{
    element = buttons.find((item) => {{
      const style = getComputedStyle(item);
      return item.getClientRects().length > 0
        && style.display !== 'none'
        && style.visibility !== 'hidden';
    }});
    strategy = 'prima chat WhatsApp visibile';
  }}
  if (!element) return {{ clicked: false, strategy: 'nessun elemento' }};
  element.click();
  return {{
    clicked: true,
    strategy,
    label: (element.innerText || element.textContent || '').trim().slice(0, 200)
  }};
}})()
"""
    click_result = await cdp.evaluate(click_expression)
    if not click_result or not click_result.get("clicked"):
        raise RuntimeError("nessuna chat WhatsApp cliccabile trovata")
    summary.append(f"chat aperta con strategia: {click_result['strategy']}")

    message_count_expression = """
(() => document.querySelectorAll(
  '#message-list .message, #message-list [class*=msg], #message-list img'
).length)()
"""
    message_count = await wait_for_condition(
        cdp, message_count_expression, "il caricamento dei messaggi"
    )
    summary.append(f"elementi messaggio caricati: {message_count}")
    await asyncio.sleep(4.0)
    summary.append("attesa thumbnail completata")

    width, height, size = await capture_screenshot(cdp, CHAT_SCREENSHOT)
    summary.append(f"{CHAT_SCREENSHOT}: {width}x{height}, {size} byte")
    return summary


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        await process.wait()
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        process.kill()
        await process.wait()


async def async_main(token: str, chrome_path: str) -> int:
    process: asyncio.subprocess.Process | None = None
    cdp: CDPClient | None = None
    try:
        # Il profilo è dedicato allo script: rimuoverlo evita lock lasciati da crash.
        if PROFILE_PATH.exists():
            await asyncio.to_thread(shutil.rmtree, PROFILE_PATH)
        process = await asyncio.create_subprocess_exec(
            chrome_path,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--remote-debugging-port=9222",
            f"--user-data-dir={PROFILE_PATH}",
            "--window-size=1400,900",
            APP_URL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        websocket_url = await wait_for_debugger(process)
        websocket = await websockets.connect(
            websocket_url, open_timeout=10.0, max_size=16 * 1024 * 1024
        )
        cdp = CDPClient(websocket)
        cdp.start()

        try:
            summary = await drive_browser(cdp, token)
        except Exception:
            await print_diagnostics(cdp)
            raise

        print("Riepilogo:")
        for step in summary:
            print(f"  OK - {step}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ERRORE: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            if cdp is not None:
                try:
                    await cdp.send_command("Browser.close", timeout=2.0)
                except Exception as exc:  # noqa: BLE001
                    print(f"Avviso durante Browser.close: {exc}", file=sys.stderr)
                try:
                    await asyncio.wait_for(cdp.close(), timeout=2.0)
                except Exception as exc:  # noqa: BLE001
                    print(f"Avviso durante la chiusura CDP: {exc}", file=sys.stderr)
        finally:
            if process is not None:
                await stop_process(process)


def main() -> int:
    args = parse_args()
    try:
        token = load_token(args.token)
        chrome_path = find_chrome()
    except RuntimeError as exc:
        print(f"ERRORE: {exc}", file=sys.stderr)
        return 1
    return asyncio.run(async_main(token, chrome_path))


if __name__ == "__main__":
    raise SystemExit(main())
