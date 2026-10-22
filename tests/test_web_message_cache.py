"""Pure logic of web/static/message_cache.js (run under node, no npm)."""

from __future__ import annotations

import subprocess


def _run(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


PRELUDE = r"""
const assert = require("node:assert/strict");
const cache = require("./web/static/message_cache.js");
const msg = (id, timestamp) => ({ id, timestamp, text: id });
const ids = (list) => list.map((m) => m.id);
"""


def test_delta_since_uses_overlap_window():
    _run(
        PRELUDE
        + r"""
assert.equal(cache.deltaSince(null), null);
assert.equal(cache.deltaSince([]), null);
const list = Array.from({ length: 50 }, (_, i) => msg(`m${i}`, i * 10));
assert.equal(cache.deltaSince(list, 30), 200);
assert.equal(cache.deltaSince(list.slice(0, 5), 30), 0);
"""
    )


def test_merge_delta_replaces_tail_keeps_older_and_trims():
    _run(
        PRELUDE
        + r"""
const cached = [msg("a", 1), msg("b", 2), msg("c", 3)];
// Tail from ts 2: "b" edited, "c" deleted server-side, "d" new.
const fresh = [{ ...msg("b", 2), text: "edited" }, msg("d", 4)];
const merged = cache.mergeDelta(cached, fresh, 2, 10);
assert.deepEqual(ids(merged), ["a", "b", "d"]);
assert.equal(merged[1].text, "edited");
assert.deepEqual(ids(cache.mergeDelta(cached, fresh, 2, 2)), ["b", "d"]);
"""
    )


def test_merge_delta_full_window_and_gap():
    _run(
        PRELUDE
        + r"""
const cached = [msg("a", 1), msg("b", 2)];
// No cache / no since: the server window is the truth.
assert.deepEqual(ids(cache.mergeDelta(null, [msg("x", 5)], null, 10)), ["x"]);
// Delta as long as the limit: there may be a hole, drop the old cache.
const fresh = [msg("c", 3), msg("d", 4)];
assert.deepEqual(ids(cache.mergeDelta(cached, fresh, 2, 2)), ["c", "d"]);
"""
    )


def test_memory_store_save_load_prune():
    _run(
        PRELUDE
        + r"""
(async () => {
  const store = cache.openMessageCache({ indexedDB: null });
  assert.equal(await store.load("signal", "a"), null);
  await store.save("signal", "a", [msg("1", 1)]);
  await new Promise((r) => setTimeout(r, 2));
  await store.save("whatsapp", "b", [msg("2", 2)]);
  assert.deepEqual(ids(await store.load("signal", "a")), ["1"]);
  await store.prune(1);
  assert.equal(await store.load("signal", "a"), null);
  assert.deepEqual(ids(await store.load("whatsapp", "b")), ["2"]);
  await store.prune(0);
  assert.equal(await store.load("whatsapp", "b"), null);
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )
