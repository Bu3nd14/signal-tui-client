# Performance Analysis — UI Reactivity

## 🟡 MEDIUM — Degrades Gradually

### R4. `_refresh_chat` sorts and iterates on every picker close
**File:** `signal_tui.py:1797`

`sorted()` on entire cache + `max()` on `_seen_timestamps`. Called when closing emoji/contact picker.

### R5. `_update_typing_label` iterates all contact list children
**File:** `signal_tui.py:860`

Linear scan of ~350 ListItem for every typing event. WhatsApp sends bursts.

**Fix:** Maintain a `dict[cache_key, ListItem]` for O(1) lookup.

### R6. `query_one("#chat-log")` called in every `_add_message`
**File:** `signal_tui.py:506`

DOM query per message mount. With 20 messages = 20 queries.

## 🟢 MINOR

### R7. `_shown_in_log` grows with chat
Cleared on chat switch — bounded per-chat, acceptable.

### R8. `_seen_timestamps` / `_seen_message_ids` grow with chat
Cleared on chat switch — bounded per-chat, but includes duplicate entries via `msg_id`.
