"""Reconciliation optimistic ↔ riga reale tramite l'id restituito da /api/send.

Caso reale (Signal, immagine con caption): la bolla optimistic
non veniva accoppiata alla riga reale e la chat mostrava due bolle finché non
si ricaricava la pagina.
"""

from __future__ import annotations

import subprocess

_PRELUDE = r"""
const assert = require("node:assert/strict");
const { reconcileOptimisticMessages, messageMediaType } = require("./web/static/reconcile.js");
const real = {
  id: "1700000000123", direction: "out", text: "caption", timestamp: 1700000000123,
  quote_text: null, quote_timestamp: null, quote_author: "",
  attachment: { attachment_id: "abc123.jpg", name: "abc123.jpg", type: "image/jpeg", media_kind: "image" },
};
"""


def _run(body: str) -> None:
    completed = subprocess.run(
        ["node", "-e", _PRELUDE + body], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_server_message_id_pairs_even_when_signatures_diverge():
    _run(
        r"""
// Mime vuoto + caption diversa: nessuna signature coincide.
const optimistic = {
  optimistic_id: "o1", protocol: "signal", contactId: "+10000000000", direction: "out",
  text: "caption ", timestamp: 1, known_message_ids: [], optimisticStatus: "sent",
  attachment: { type: "", name: "upload-abc", attachment_id: "upload-abc", media_kind: null },
  server_message_id: "1700000000123",
};
const result = reconcileOptimisticMessages([real], [optimistic], "signal", "+10000000000");
assert.equal(result.optimistic[0].confirmed_message_id, "1700000000123");
assert.equal(result.visible.length, 0);
"""
    )


def test_server_message_id_absent_from_page_keeps_bubble():
    _run(
        r"""
const optimistic = {
  optimistic_id: "o1", protocol: "signal", contactId: "+10000000000", direction: "out",
  text: "altro", timestamp: 1, known_message_ids: [], optimisticStatus: "sent",
  server_message_id: "999",
};
const result = reconcileOptimisticMessages([real], [optimistic], "signal", "+10000000000");
assert.equal(result.optimistic[0].confirmed_message_id, undefined);
assert.equal(result.visible.length, 1);
"""
    )


def test_server_message_id_claims_row_before_signature_twin():
    _run(
        r"""
// Due invii identici: ciascuno prende la SUA riga, non la prima per signature.
const older = { ...real, id: "100", timestamp: 100 };
const newer = { ...real, id: "200", timestamp: 200 };
const base = { protocol: "signal", contactId: "+10000000000", direction: "out", text: "caption",
  known_message_ids: [], optimisticStatus: "sent",
  attachment: { type: "image/jpeg", name: "a.jpg", media_kind: "image" } };
const a = { ...base, optimistic_id: "a", timestamp: 300, server_message_id: "100" };
const b = { ...base, optimistic_id: "b", timestamp: 301, server_message_id: "200" };
const result = reconcileOptimisticMessages([older, newer], [a, b], "signal", "+10000000000");
const byId = Object.fromEntries(result.optimistic.map((o) => [o.client_key ?? o.server_message_id, o.confirmed_message_id]));
assert.deepEqual(byId, { 100: "100", 200: "200" });
"""
    )


def test_media_type_falls_back_to_filename_extension():
    _run(
        r"""
assert.equal(messageMediaType({ attachment: { type: "", name: "photo.JPG" } }), "image");
assert.equal(messageMediaType({ attachment: { type: "", name: "clip.mp4" } }), "video");
assert.equal(messageMediaType({ attachment: { type: "", name: "doc.pdf" } }), "attachment");
assert.equal(messageMediaType({ attachment: { type: "", name: "upload-xyz" } }), "attachment");
"""
    )
