# Performance Analysis — UI Reactivity

## 🔴 CRITICAL — Blocks UI Thread

### R2. `_update_message_widgets_status` O(N×M) scan per receipt
**File:** `signal_tui.py:776-793`

Nested loop: for each updated receipt, scans all chat_log children. With 200 visible messages, a 2-id receipt = 400 comparisons.

### R3. `on_input_changed` → `show_suggestions` → `_rebuild` on every keystroke
**File:** `emoji_picker.py:588-601` + `signal_tui.py:1568-1587`

Each keystroke while typing emoji alias triggers `remove_children()` + up to 10 `mount()`. 10 layout passes per keystroke.

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
