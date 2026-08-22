# DESIGN — Contatore unread per backend nella status bar

> Verifica dello stato dell'arte: architetto. Design operativo: orchestratore (le decisioni di UX sono state approvate dall'utente).
> Branch: `feat/status-backend-unread` · Base: master `889a961` · Problema: in vista filtrata/raggruppata un contatto che esiste solo su un backend può passare inosservato quando riceve messaggi.

## 0. Stato dell'arte (verificato)

| Elemento | Riferimento | Fatto |
|---|---|---|
| `_status(text, duration=3.0)` | `tui/app.py:261-279` | Scrive in `#status-bar`, cancella il timer precedente (`:269-271`), arma `set_timer(duration, _status_clear)` solo se `duration > 0 and self.is_running` (`:276-277`) |
| `_status_clear()` | `tui/app.py:281-287` | Scrive `""` e azzera `_status_timer` |
| `#status-bar` | `tui/app.py:205`; `tui/css.py:263-267` | `Static` in `#bottom-bar` (dock bottom, height 1), **width fissa 45**, `text-align: right` |
| `_unread_counts` | `tui/app.py:116` | `dict[str, int]` keyed da `contact_cache_key` = `f"{protocol}:{id}"` (`models.py:55`) |
| `protocol_emoji` | `models.py:24-31` | `📱` signal, `💬` whatsapp, `📨` telegram |
| Flush batch | `tui/polling.py:71-88` | Già su UI thread via `call_from_thread`: `_recompute_unread(k)` incrementale (`:80`) o full (`:84`), poi `_reorder_contact_list` (`:88`) |
| `_update_unread_badges` | `tui/unread_reply.py:64-83` | Unico chiamante: `_on_backend_ready` (`backend_connect.py:124`) |
| Azzeramento su selezione | `tui/contacts.py:685` | `self._unread_counts[cache_key] = 0` in `_select_contact` |
| Status di avvio | `backend_connect.py:134,158,164,166,169,173,188,199,211,221,236,244,254,256,273,283,290` | Mix di persistenti (`duration=0`) e transienti |

## 1. Formato del contenuto di default

```
📱 N  💬 N  📨 N
```
- Ordine **fisso**: Signal → WhatsApp → Telegram (`protocol_emoji`).
- `N` = somma degli unread di tutti i contatti di quel protocollo; `-` se 0.
- **Sempre mostrato** (anche con tutti a 0): la barra "vive" e informa stabilmente; entro la width 45 (3×~9 char) rientra senza troncamenti.
- Helper: `_render_backend_unread_status()` in `app.py` che scrive il default nel widget.

## 2. Meccanica di precedenza (default < transiente < errore permanente)

- **Nuovo stato**: `self._status_active: bool = False` (True se un messaggio è in mostra).
- `_status(text, duration)` (invariato nel resto): setta `_status_active = True`; scrive nel widget; cancella il timer precedente; se `duration > 0 and self.is_running` arma `set_timer(duration, _status_clear)`.
- `_status_clear()`: `_status_active = False`; `_status_timer = None`; **ripristina il default** con `_render_backend_unread_status()` (al posto di scrivere `""`).
- **Errori permanenti** (`duration=0`): restano in mostra finché il codice non li pulisce (via una nuova `_status(...)` o `_status_clear()`); il default riappare al primo `_status_clear`.
- Nessun nuovo dismiss UI esplicito (fuori scope): il percorso `_status_clear` è sufficiente.

## 3. Calcolo dei totali per backend

Dalla fonte autorevole: `self.contacts` + `_unread_counts`.
```python
def _backend_unread_total(self, protocol: str) -> int:
    return sum(
        self._unread_counts.get(c.cache_key, 0)
        for c in self.contacts
        if c.protocol == protocol
    )
```
- O(N) con N≈350 contatti, solo al refresh — irrilevante.
- Riusa `protocol_emoji` e l'ordine `(PROTOCOL_SIGNAL, PROTOCOL_WHATSAPP, PROTOCOL_TELEGRAM)`.

## 4. Punti di refresh (nessun lampeggio sopra messaggi attivi)

- **Helper** `_refresh_backend_status_if_idle()`: se `not self._status_active` → `_render_backend_unread_status()`.
- Chiamato in:
  1. **Flush del polling** (`tui/polling.py:71-88`): dopo `_reorder_contact_list` (fine batch, già su UI thread).
  2. **`_select_contact`** (`tui/contacts.py:~685`): dopo `self._unread_counts[cache_key] = 0`.
  3. **`_on_backend_ready`** (`backend_connect.py:~124`): dopo `_update_unread_badges()` (innesco di avvio del default).
- Thread-safety: tutti i punti girano già sul thread UI; il refresh è leggero.

## 5. Sequenza di avvio

- Gli status di avvio (transienti e persistenti) sovrascrivono la barra.
- Il **default compare automaticamente** al primo `_status_clear` (timer di un transiente scaduto) perché `_status_clear` ora ripristina il default.
- **Innesco iniziale**: `_on_backend_ready` chiama `_refresh_backend_status_if_idle()` dopo `_update_unread_badges()` → se non ci sono altri messaggi attivi, il default appare subito (e gli status successivi lo sovrascrivono, per poi ripristinarlo).

## 6. Test e coverage

File: `tests/test_status_backend_unread.py` (nuovo) + aggiornamenti a eventuali test esistenti che asseriscono `_status_clear` → `""`.

Casi:
1. `_render_backend_unread_status`: default con 0 backend con unread (`📱 -  💬 -  📨 -`), con conteggi su 1/2/3 backend, ordine fisso.
2. `_status_clear` ripristina il default (non più vuoto).
3. `_status` transiente sovrascrive; allo scadere del timer → default ripristinato e aggiornato.
4. Errore permanente (`duration=0`): default nascosto; dopo `_status_clear` → default.
5. `_refresh_backend_status_if_idle`: con `_status_active=True` NON tocca la barra; con `False` aggiorna.
6. `_select_contact`/flush: il default riflette l'azzeramento dopo la lettura.
7. Avvio: dopo `_on_backend_ready` il default è visibile (o comunque il primo `_status_clear` lo ripristina).

Coverage: tutte le nuove branch di `_render_backend_unread_status`, `_refresh_backend_status_if_idle`, `_backend_unread_total`, e i rami modificati di `_status_clear` coperti.

## 7. Rischio / regressioni

- **Test esistenti che asseriscono lo svuotamento**: eventuali test che verificano `_status_clear` → `update("")` vanno aggiornati (ora → default). Individuarli e aggiornarli.
- `_status`/timer: logica invariata, solo aggiunta di `_status_active` e del restore nel clear.
- Nessun impatto su backend, schema, contact list.

## Cosa NON fare

- Non aggiungere UI di dismiss per gli errori permanenti (fuori scope).
- Non mostrare il default sopra un messaggio attivo (rispettare sempre `_status_active`).
- Non toccare il formato del messaggio `_status` (resta come oggi).
