"""Frontend multi-attachment (design docs/DESIGN_WEB_MULTI_ATTACHMENT.md §6, step 3).

Test JS stile ``node -e`` (pattern di tests/test_web_phase2_fixes.py): la
passata dedicata di reconciliation per gli optimistic multi (``batch_id``),
la non-regressione del single-attachment, lo staging multiplo con rimozione
singola e l'invio con N optimistic + FormData multi-file.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest


def _run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_reconcile_multi_attachment_same_name_pairs_by_batch_slot():
    """3 allegati omonimi per categoria: signature identiche per gli indici
    1 e 2, quindi il pairing deve venire dallo slot batch_id+batch_index
    (deterministico), non dalla signature (arbitrario)."""
    _run_node(r"""
const assert = require("node:assert/strict");
const { reconcileOptimisticMessages } = require("./web/static/reconcile.js");
const optimistic = [0, 1, 2].map((index) => ({
  optimistic_id: `batch-1-att${index}`, batch_id: "batch-1", batch_index: index,
  protocol: "whatsapp", contactId: "42", direction: "out",
  text: index === 0 ? "vacanze" : "", timestamp: 5000,
  known_message_ids: [], optimisticStatus: "sent",
  attachment: { type: "image/jpeg", name: "foto.png", attachment_id: `foto.png[${index}]` },
}));
// Righe reali in ordine sparso: un pairing per signature (exactMatches.find
// in ordine di inserimento) accopperebbe optimistic 1->wa-2 e 2->wa-1.
const real = [
  { id: "wa-2", direction: "out", text: "", timestamp: 5001, batch_id: "batch-1", batch_index: 2, attachment: { type: "image/jpeg", name: "foto.png", attachment_id: "remote-2" } },
  { id: "wa-0", direction: "out", text: "vacanze", timestamp: 5001, batch_id: "batch-1", batch_index: 0, attachment: { type: "image/jpeg", name: "foto.png", attachment_id: "remote-0" } },
  { id: "wa-1", direction: "out", text: "", timestamp: 5001, batch_id: "batch-1", batch_index: 1, attachment: { type: "image/jpeg", name: "foto.png", attachment_id: "remote-1" } },
];
const result = reconcileOptimisticMessages(real, optimistic, "whatsapp", "42");
assert.equal(result.visible.length, 0);
const byIndex = (index) => result.optimistic.find((item) => item.batch_index === index);
assert.equal(byIndex(0).confirmed_message_id, "wa-0");
assert.equal(byIndex(1).confirmed_message_id, "wa-1");
assert.equal(byIndex(2).confirmed_message_id, "wa-2");
""")


def test_reconcile_multi_attachment_signal_rows_share_msg_id():
    """Signal materializza le N righe di un batch con lo STESSO msg_id: la
    passata dedicata deve confermarle tutte (claiming per slot, non per
    identity, che sarebbe ambigua e bloccherebbe gli indici 1 e 2)."""
    _run_node(r"""
const assert = require("node:assert/strict");
const { reconcileOptimisticMessages } = require("./web/static/reconcile.js");
const optimistic = [0, 1, 2].map((index) => ({
  optimistic_id: `batch-1-att${index}`, batch_id: "batch-1", batch_index: index,
  protocol: "signal", contactId: "42", direction: "out",
  text: index === 0 ? "batch caption" : "", timestamp: 5000,
  known_message_ids: [], optimisticStatus: "sent",
  attachment: { type: "image/png", name: "foto.png", attachment_id: `foto.png[${index}]` },
}));
const real = [0, 1, 2].map((index) => ({
  id: "1787250931234", direction: "out", text: index === 0 ? "batch caption" : "",
  timestamp: 5001, batch_id: "batch-1", batch_index: index,
  attachment: { type: "image/png", name: "foto.png", attachment_id: `mirror-${index}` },
}));
const result = reconcileOptimisticMessages(real, optimistic, "signal", "42");
assert.equal(result.visible.length, 0);
assert.equal(result.optimistic.length, 3);
for (const item of result.optimistic) {
  assert.equal(item.confirmed_message_id, "1787250931234");
}
""")


def test_reconcile_single_attachment_signature_and_pairing_unchanged():
    """Non-regressione single: messageSignature ignora i campi batch e il
    pairing resta quello del loop generico (nessun batch_id)."""
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
// messageSignature non e' esportato: si carica reconcile.js in questo
// contesto (le function top-level diventano globali, come nei test vm).
vm.runInThisContext(fs.readFileSync("./web/static/reconcile.js", "utf8"));
const single = {
  direction: "out", text: "", timestamp: 1000,
  attachment: { type: "image/png", name: "foto.png", attachment_id: "foto.png[0]", media_kind: "image" },
};
// I campi batch non entrano nella signature: il single non cambia faccia.
assert.equal(messageSignature({ ...single, batch_id: "batch-1", batch_index: 2 }), messageSignature(single));
const optimistic = {
  optimistic_id: "local", protocol: "signal", contactId: "42", direction: "out",
  text: "", timestamp: 9000, known_message_ids: [], optimisticStatus: "sent",
  attachment: { type: "image/png", name: "foto.png", attachment_id: "foto.png[0]" },
};
const echo = {
  id: "sig-1", direction: "out", text: "", timestamp: 1000,
  attachment: { type: "image/jpeg", name: "foto.png", attachment_id: "remote" },
};
const result = reconcileOptimisticMessages([echo], [optimistic], "signal", "42");
assert.equal(result.visible.length, 0);
assert.equal(result.optimistic[0].confirmed_message_id, "sig-1");
""")


def test_reconcile_multi_pass_runs_first_and_protects_batch_rows():
    """La passata multi gira PRIMA del loop generico: un optimistic single
    con la STESSA signature delle righe batch (omonimi) non puo' rubarle."""
    _run_node(r"""
const assert = require("node:assert/strict");
const { reconcileOptimisticMessages } = require("./web/static/reconcile.js");
const batchOptimistic = [0, 1, 2].map((index) => ({
  optimistic_id: `batch-1-att${index}`, batch_id: "batch-1", batch_index: index,
  protocol: "whatsapp", contactId: "42", direction: "out", text: "", timestamp: 5000,
  known_message_ids: [], optimisticStatus: "sent",
  attachment: { type: "image/jpeg", name: "foto.png", attachment_id: `foto.png[${index}]` },
}));
const singleOptimistic = {
  optimistic_id: "local-single", protocol: "whatsapp", contactId: "42", direction: "out",
  text: "", timestamp: 6000, known_message_ids: [], optimisticStatus: "sent",
  attachment: { type: "image/jpeg", name: "foto.png", attachment_id: "foto.png[0]" },
};
const real = [
  ...[0, 1, 2].map((index) => ({
    id: `wa-${index}`, direction: "out", text: "", timestamp: 5001,
    batch_id: "batch-1", batch_index: index,
    attachment: { type: "image/jpeg", name: "foto.png", attachment_id: `remote-${index}` },
  })),
  { id: "wa-9", direction: "out", text: "", timestamp: 6001, attachment: { type: "image/jpeg", name: "foto.png", attachment_id: "single-remote" } },
];
const result = reconcileOptimisticMessages(real, [singleOptimistic, ...batchOptimistic], "whatsapp", "42");
assert.equal(result.visible.length, 0);
// Il confermato perde optimistic_id: il single si riconosce dal batch_id null.
const reconciledSingle = result.optimistic.find((item) => item.batch_id == null);
assert.equal(reconciledSingle.confirmed_message_id, "wa-9");
const byIndex = (index) => result.optimistic.find((item) => item.batch_index === index && item.batch_id === "batch-1");
assert.equal(byIndex(0).confirmed_message_id, "wa-0");
assert.equal(byIndex(1).confirmed_message_id, "wa-1");
assert.equal(byIndex(2).confirmed_message_id, "wa-2");
""")


def test_reconcile_single_precedes_multi_fallback_on_shared_signature():
    """R2: a parita' di signature il single conserva il match generico anche
    se un multi fallback e' piu' recente, perche' i single si processano
    prima (niente sort unico combinato)."""
    _run_node(r"""
const assert = require("node:assert/strict");
const { reconcileOptimisticMessages } = require("./web/static/reconcile.js");
const real = {
  id: "real-1", direction: "out", text: "", timestamp: 1000,
  attachment: { type: "image/png", name: "same.png", attachment_id: "remote", media_kind: "image" },
};
const single = {
  optimistic_id: "single", batch_id: null, protocol: "whatsapp", contactId: "42",
  direction: "out", text: "", timestamp: 2000, known_message_ids: [],
  optimisticStatus: "sent",
  attachment: { type: "image/png", name: "same.png", attachment_id: "same.png[0]" },
};
// Multi senza slot reale: finisce nei fallback, con timestamp piu' recente.
const multi = {
  optimistic_id: "multi", batch_id: "missing-batch", batch_index: 0,
  protocol: "whatsapp", contactId: "42", direction: "out", text: "",
  timestamp: 3000, known_message_ids: [], optimisticStatus: "sent",
  attachment: { type: "image/png", name: "same.png", attachment_id: "same.png[1]" },
};
const result = reconcileOptimisticMessages([real], [single, multi], "whatsapp", "42");
// Il single (batch_id null) ha conservato il match.
const reconciledSingle = result.optimistic.find((item) => item.batch_id == null);
assert.equal(reconciledSingle.confirmed_message_id, "real-1");
// Un solo orfano: il multi fallback, non il single.
assert.equal(result.visible.length, 1);
assert.equal(result.visible[0].optimistic_id, "multi");
assert.equal(result.visible[0].confirmed_message_id, undefined);
""")


def test_submit_message_multi_attachment_builds_batch_optimistics_and_formdata():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const helper = app.slice(app.indexOf("function mediaKindFromMime("), app.indexOf("\nfunction clearStagedAttachments"));
const submit = helper + "\n" + app.slice(app.indexOf("async function submitMessage("), app.indexOf("\nfunction encodeToken"));
globalThis.state = {
  sending: 0,
  active: { id: "alice", protocol: "signal" },
  stagedAttachments: [
    { file: new Blob(["a"], { type: "image/png" }), filename: "foto.png", previewUrl: "blob:0", previewWidth: 640, previewHeight: 480, attachmentId: "foto.png[0]" },
    { file: new Blob(["b"], { type: "image/png" }), filename: "foto.png", previewUrl: "blob:1", previewWidth: 320, previewHeight: 240, attachmentId: "foto.png[1]" },
  ],
  replyTo: null,
  messages: [],
  optimistic: [],
  optimisticSequence: 0,
};
globalThis.elements = { messageInput: { value: "due foto", focus() {} } };
globalThis.window = { SignalTuiReconcile: { messageIdentity: (message) => message.id } };
globalThis.resizeComposer = () => {};
globalThis.updateComposer = () => {};
globalThis.renderMessages = () => {};
const cached = [];
globalThis.cacheMedia = (key, url, width, height) => cached.push([key, url, width, height]);
let clearedWithOptions = null;
globalThis.clearStagedAttachments = (options) => { clearedWithOptions = options; state.stagedAttachments = []; };
globalThis.showError = assert.fail;
let request;
globalThis.apiFetch = async (url, options) => { request = { url, options }; return { status: 200 }; };
vm.runInThisContext(submit);
(async () => {
  await submitMessage();
  assert.equal(state.optimistic.length, 2);
  const batchId = state.optimistic[0].batch_id;
  assert.ok(batchId, "batch_id atteso per il multi-allegato");
  assert.equal(state.optimistic[0].batch_id, batchId);
  assert.equal(state.optimistic[0].batch_index, 0);
  assert.equal(state.optimistic[1].batch_id, batchId);
  assert.equal(state.optimistic[1].batch_index, 1);
  assert.equal(state.optimistic[0].optimistic_id, `${batchId}-att0`);
  assert.equal(state.optimistic[1].optimistic_id, `${batchId}-att1`);
  // Caption solo sul primo allegato (come le righe reali: captions[0]).
  assert.equal(state.optimistic[0].text, "due foto");
  assert.equal(state.optimistic[1].text, "");
  assert.equal(state.optimistic[0].attachment.attachment_id, "foto.png[0]");
  assert.equal(state.optimistic[1].attachment.attachment_id, "foto.png[1]");
  assert.equal(state.optimistic[0].localPreviewUrl, "blob:0");
  assert.equal(state.optimistic[1].localPreviewUrl, "blob:1");
  assert.equal(state.optimistic[0].optimisticStatus, "sent");
  assert.equal(state.optimistic[1].optimisticStatus, "sent");
  assert.deepEqual(cached, [
    ["foto.png[0]", "blob:0", 640, 480],
    ["foto.png[1]", "blob:1", 320, 240],
  ]);
  // Il path di invio NON revoca i blob serviti all'optimistic.
  assert.deepEqual(clearedWithOptions, { revoke: false });
  assert.equal(request.url, "/api/send");
  const body = request.options.body;
  assert.equal(body.get("protocol"), "signal");
  assert.equal(body.get("contact_id"), "alice");
  assert.equal(body.get("text"), "due foto");
  assert.equal(body.get("batch_id"), batchId);
  assert.equal(body.getAll("file").length, 2);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_stage_attachments_previews_and_single_removal():
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const media = app.slice(app.indexOf("const MEDIA_CACHE_LIMIT"), app.indexOf("\nfunction attachmentName"));
// Unico slice: mediaKindFromMime + MAX_STAGED_ATTACHMENTS + tutto lo staging.
const stage = app.slice(app.indexOf("function mediaKindFromMime("), app.indexOf("\nasync function submitMessage"));
globalThis.state = { stagedAttachments: [], mediaCache: new Map(), mediaFailures: new Set(), objectUrls: new Set() };
const revoked = [];
let urlSequence = 0;
globalThis.URL = { createObjectURL: () => `blob:${urlSequence++}`, revokeObjectURL: (url) => revoked.push(url) };
const images = [];
function node(tag) {
  const value = {
    tag, className: "", children: [], textContent: "", title: "", type: "", alt: "", src: "", width: 0, height: 0,
    append(...items) { this.children.push(...items); },
    addEventListener(_name, callback) { this.callback = callback; },
    setAttribute() {},
  };
  if (tag === "img") images.push(value);
  return value;
}
globalThis.document = {
  createElement: (tag) => tag === "canvas"
    ? { getContext: () => ({ fillRect() {}, drawImage() {}, set fillStyle(_value) {} }), toBlob: (cb) => cb({ size: 1000 }) }
    : node(tag),
};
globalThis.createImageBitmap = async (file) => ({ width: file.width, height: file.height, close() {} });
globalThis.elements = {
  attachmentsPreview: { children: [], hidden: true, replaceChildren(...items) { this.children = items; }, append(item) { this.children.push(item); } },
};
const errors = [];
globalThis.showError = (message) => errors.push(message);
globalThis.updateComposer = () => {};
vm.runInThisContext(media);
vm.runInThisContext(stage);
(async () => {
  await stageAttachments([
    { type: "image/png", size: 1000, name: "foto.png", width: 640, height: 480 },
    { type: "image/png", size: 1000, name: "foto.png", width: 320, height: 240 },
    { type: "application/pdf", size: 1000, name: "doc.pdf" },
  ]);
  assert.deepEqual(errors, []);
  assert.equal(state.stagedAttachments.length, 3);
  // Omonimi: chiavi per indice, entry distinte nella mediaCache.
  assert.equal(state.stagedAttachments[0].attachmentId, "foto.png[0]");
  assert.equal(state.stagedAttachments[1].attachmentId, "foto.png[1]");
  assert.equal(state.stagedAttachments[2].attachmentId, "doc.pdf[2]");
  assert.equal(state.mediaCache.get("foto.png[0]").url, "blob:0");
  assert.equal(state.mediaCache.get("foto.png[0]").width, 640);
  assert.equal(state.mediaCache.get("foto.png[1]").url, "blob:1");
  assert.equal(state.mediaCache.has("doc.pdf[2]"), false);
  // Preview multipli: item con img per le immagini, icona per il documento.
  const container = elements.attachmentsPreview;
  assert.equal(container.hidden, false);
  assert.equal(container.children.length, 3);
  assert.equal(container.children[0].children[0].children[0].src, "blob:0");
  assert.equal(container.children[1].children[0].children[0].src, "blob:1");
  assert.equal(container.children[2].children[0].children[0].textContent, "📎");
  // Rimozione singola (via handler dinamico del secondo item): revoca SOLO
  // l'URL rimosso e scarta il relativo seeding.
  container.children[1].children[2].callback();
  assert.equal(state.stagedAttachments.length, 2);
  assert.deepEqual(revoked, ["blob:1"]);
  assert.equal(state.mediaCache.has("foto.png[1]"), false);
  assert.equal(state.mediaCache.get("foto.png[0]").url, "blob:0");
  assert.equal(container.children.length, 2);
  removeStagedAttachment(0);
  assert.equal(state.stagedAttachments.length, 1);
  assert.deepEqual(revoked, ["blob:1", "blob:0"]);
  clearStagedAttachments();
  assert.equal(state.stagedAttachments.length, 0);
  assert.equal(container.hidden, true);
  assert.deepEqual(revoked, ["blob:1", "blob:0"]);
  // Path di invio: revoke:false non revoca e non scarta il seeding.
  await stageAttachments([{ type: "image/png", size: 1000, name: "sola.png", width: 100, height: 100 }]);
  const url = state.stagedAttachments[0].previewUrl;
  clearStagedAttachments({ revoke: false });
  assert.equal(state.stagedAttachments.length, 0);
  assert.deepEqual(revoked, ["blob:1", "blob:0"]);
  assert.equal(state.mediaCache.get("sola.png[0]").url, url);
  // Oltre il cap del backend lo staging si ferma a 10 con errore.
  const files = Array.from({ length: 12 }, (_value, index) => ({ type: "text/plain", size: 10, name: `f${index}.txt` }));
  state.stagedAttachments = [];
  await stageAttachments(files);
  assert.equal(state.stagedAttachments.length, 10);
  assert.deepEqual(errors, ["Puoi allegare al massimo 10 file per messaggio."]);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_stage_same_name_remove_readd_does_not_reuse_attachment_id():
    """FIX-2 (report tester §5.2): remove+re-add di un omonimo non riusa la
    chiave del sopravvissuto. La chiave calcolata dalla LUNGHEZZA corrente si
    riavvolge dopo una rimozione: re-add di ``foto.png`` con un solo allegato
    staged riusava ``foto.png[1]`` del sopravvissuto → chiavi duplicate,
    revoca del suo object URL in cacheMedia, preview rotta e dims perse. Il
    fix genera l'indice come il più piccolo LIBERO tra gli attachmentId
    correnti (atomico rispetto al push: vale anche per due stageAttachments
    concorrenti)."""
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const media = app.slice(app.indexOf("const MEDIA_CACHE_LIMIT"), app.indexOf("\nfunction attachmentName"));
const stage = app.slice(app.indexOf("function mediaKindFromMime("), app.indexOf("\nasync function submitMessage"));
globalThis.state = { stagedAttachments: [], mediaCache: new Map(), mediaFailures: new Set(), objectUrls: new Set() };
const revoked = [];
let urlSequence = 0;
globalThis.URL = { createObjectURL: () => `blob:${urlSequence++}`, revokeObjectURL: (url) => revoked.push(url) };
function node(tag) {
  const value = {
    tag, className: "", children: [], textContent: "", title: "", type: "", alt: "", src: "", width: 0, height: 0,
    append(...items) { this.children.push(...items); },
    addEventListener(_name, callback) { this.callback = callback; },
    setAttribute() {},
  };
  return value;
}
globalThis.document = {
  createElement: (tag) => tag === "canvas"
    ? { getContext: () => ({ fillRect() {}, drawImage() {}, set fillStyle(_value) {} }), toBlob: (cb) => cb({ size: 1000 }) }
    : node(tag),
};
globalThis.createImageBitmap = async (file) => ({ width: file.width, height: file.height, close() {} });
globalThis.elements = {
  attachmentsPreview: { children: [], hidden: true, replaceChildren(...items) { this.children = items; }, append(item) { this.children.push(item); } },
};
const errors = [];
globalThis.showError = (message) => errors.push(message);
globalThis.updateComposer = () => {};
vm.runInThisContext(media);
vm.runInThisContext(stage);
(async () => {
  await stageAttachments([
    { type: "image/png", size: 1000, name: "foto.png", width: 640, height: 480 },
    { type: "image/png", size: 1000, name: "foto.png", width: 320, height: 240 },
  ]);
  assert.deepEqual(errors, []);
  const [first, second] = state.stagedAttachments;
  assert.equal(first.attachmentId, "foto.png[0]");
  assert.equal(second.attachmentId, "foto.png[1]");
  // Remove del primo: il sopravvissuto resta keyed sulla SUA chiave.
  removeStagedAttachment(0);
  const survivor = state.stagedAttachments[0];
  assert.equal(survivor, second);
  assert.equal(survivor.attachmentId, "foto.png[1]");
  // Re-add dell'omonimo: la lunghezza dell'array si e' riavvolta a 1, ma la
  // nuova chiave NON riusa quella del sopravvissuto.
  await stageAttachments([{ type: "image/png", size: 1000, name: "foto.png", width: 160, height: 120 }]);
  assert.equal(state.stagedAttachments.length, 2);
  const ids = state.stagedAttachments.map((item) => item.attachmentId);
  assert.equal(new Set(ids).size, ids.length, `chiavi attachmentId duplicate: ${ids}`);
  // L'object URL del sopravvissuto NON viene revocato dal re-cache dell'omonimo.
  assert.equal(revoked.includes(survivor.previewUrl), false);
  assert.deepEqual(revoked, [first.previewUrl]);
  // La entry mediaCache del sopravvissuto punta ancora alla SUA preview, dims comprese.
  const survivorEntry = state.mediaCache.get(survivor.attachmentId);
  assert.equal(survivorEntry.url, survivor.previewUrl);
  assert.equal(survivorEntry.width, 320);
  assert.equal(survivorEntry.height, 240);
  // Due stageAttachments concorrenti con omonimi: il calcolo dell'indice
  // libero e' atomico rispetto al push → chiavi comunque uniche.
  state.stagedAttachments = [];
  state.mediaCache.clear();
  await Promise.all([
    stageAttachments([{ type: "image/png", size: 1000, name: "clone.png", width: 10, height: 10 }]),
    stageAttachments([{ type: "image/png", size: 1000, name: "clone.png", width: 20, height: 20 }]),
  ]);
  const concurrentIds = state.stagedAttachments.map((item) => item.attachmentId);
  assert.equal(new Set(concurrentIds).size, concurrentIds.length, `chiavi attachmentId duplicate: ${concurrentIds}`);
  assert.deepEqual(errors, []);
})().catch((error) => { console.error(error); process.exitCode = 1; });
""")


def test_render_delivers_local_previews_per_batch_slot():
    """Signal: N righe confermate con lo STESSO msg_id — il deliver del blob
    optimistic usa lo slot batch, altrimenti tutte le preview finirebbero
    sulla prima riga (scambio preview)."""
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const reconcile = fs.readFileSync("./web/static/reconcile.js", "utf8");
const media = app.slice(app.indexOf("const MEDIA_CACHE_LIMIT"), app.indexOf("\nfunction attachmentName"));
const render = app.slice(app.indexOf("function messageNodeKey("), app.indexOf("\nasync function loadMessages"));
globalThis.state = {
  mediaRequests: new Set(), mediaLoads: new Map(), mediaFailures: new Set(),
  objectUrls: new Set(), mediaCache: new Map(),
  optimistic: [0, 1, 2].map((index) => ({
    confirmed_message_id: "1787250931234", batch_id: "batch-1", batch_index: index,
    localPreviewUrl: `blob:${index}`, known_message_ids: [],
    protocol: "signal", contactId: "42", direction: "out", text: "", timestamp: 5,
    attachment: { type: "image/png", name: "foto.png", attachment_id: `foto.png[${index}]` },
  })),
  active: { protocol: "signal", id: "42" }, userScrolledUp: false,
};
const revoked = [];
globalThis.URL = { createObjectURL: () => "blob:fetched", revokeObjectURL: (url) => revoked.push(url) };
globalThis.scrollThreadToBottom = () => {};
globalThis.timestampMilliseconds = (value) => Number(value);
globalThis.formatTimestamp = () => "";
globalThis.appendRenderedQuote = () => {};
const images = [];
function node(tag) {
  const value = {
    tag, className: "", children: [], textContent: "", title: "",
    append(...items) { this.children.push(...items); },
    addEventListener() {}, setAttribute() {}, remove() {},
  };
  if (tag === "img") images.push(value);
  return value;
}
globalThis.document = { createElement: node };
globalThis.elements = { messages: { children: [], replaceChildren(...items) { this.children = items; }, append(item) { this.children.push(item); }, scrollTop: 0, scrollHeight: 0, clientHeight: 0 } };
globalThis.window = {};
vm.runInThisContext(reconcile);
// Seeding come da submitMessage: chiave per indice con dims.
state.mediaCache.set("foto.png[0]", { url: "blob:0", width: 640, height: 480 });
state.mediaCache.set("foto.png[1]", { url: "blob:1", width: 320, height: 240 });
state.mediaCache.set("foto.png[2]", { url: "blob:2", width: 160, height: 120 });
const messages = [0, 1, 2].map((index) => ({
  id: "1787250931234", direction: "out", text: "", timestamp: 6,
  batch_id: "batch-1", batch_index: index,
  attachment: { type: "image/png", media_kind: "image", name: "foto.png", attachment_id: `mirror-${index}` },
}));
vm.runInThisContext(media);
vm.runInThisContext(render);
renderMessages(messages, "signal");
// Ogni riga ha la preview del PROPRIO slot (nessuno scambio).
assert.equal(messages[0].localPreviewUrl, "blob:0");
assert.equal(messages[1].localPreviewUrl, "blob:1");
assert.equal(messages[2].localPreviewUrl, "blob:2");
assert.equal(state.mediaCache.get("mirror-0").url, "blob:0");
assert.equal(state.mediaCache.get("mirror-1").url, "blob:1");
assert.equal(state.mediaCache.get("mirror-2").url, "blob:2");
assert.equal(state.mediaCache.get("mirror-0").width, 640);
for (const item of state.optimistic) assert.equal(item.localPreviewUrl, undefined);
assert.equal(images.length, 3);
assert.deepEqual(images.map((image) => image.src), ["blob:0", "blob:1", "blob:2"]);
assert.deepEqual(images.map((image) => image.width), [640, 320, 160]);
assert.deepEqual(revoked, []);
""")


def test_confirmed_message_index_falls_back_to_confirmed_id():
    """R3: se lo slot batch non esiste la lookup ricade su
    confirmed_message_id (multi riconciliato via fallback)."""
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("function confirmedMessageIndex(");
const end = app.indexOf("\nfunction ", start + 1);
globalThis.window = {
  SignalTuiReconcile: { messageIdentity: (message, index) => String(message.id ?? index) },
};
vm.runInThisContext(app.slice(start, end));
const messages = [{ id: "other" }, { id: "real-1" }];
// Slot assente -> fallback su confirmed_message_id.
assert.equal(
  confirmedMessageIndex({ batch_id: "b1", batch_index: 3, confirmed_message_id: "real-1" }, messages),
  1,
);
// Slot presente -> priorita' al pairing batch_id+batch_index.
const slotted = [{ id: "real-x", batch_id: "b1", batch_index: 0 }, { id: "real-1" }];
assert.equal(
  confirmedMessageIndex({ batch_id: "b1", batch_index: 0, confirmed_message_id: "real-1" }, slotted),
  0,
);
// Single invariato (nessun batch -> identity).
assert.equal(confirmedMessageIndex({ batch_id: null, confirmed_message_id: "real-1" }, messages), 1);
assert.equal(confirmedMessageIndex({ batch_id: null, confirmed_message_id: "nope" }, messages), -1);
""")


def test_render_delivers_preview_when_multi_confirmed_without_batch_slot():
    """R3: un multi riconciliato via fallback (riga reale senza batch) usa
    confirmed_message_id e trasferisce comunque la preview locale."""
    _run_node(r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const reconcile = fs.readFileSync("./web/static/reconcile.js", "utf8");
const media = app.slice(app.indexOf("const MEDIA_CACHE_LIMIT"), app.indexOf("\nfunction attachmentName"));
const render = app.slice(app.indexOf("function messageNodeKey("), app.indexOf("\nasync function loadMessages"));
globalThis.state = {
  mediaRequests: new Set(), mediaLoads: new Map(), mediaFailures: new Set(),
  objectUrls: new Set(), mediaCache: new Map(),
  optimistic: [{
    confirmed_message_id: "real-1", batch_id: "batch-1", batch_index: 0,
    localPreviewUrl: "blob:0", known_message_ids: [],
    protocol: "telegram", contactId: "42", direction: "out", text: "", timestamp: 5,
    attachment: { type: "image/png", name: "foto.png", attachment_id: "foto.png[0]" },
  }],
  active: { protocol: "telegram", id: "42" }, userScrolledUp: false,
};
const revoked = [];
globalThis.URL = { createObjectURL: () => "blob:fetched", revokeObjectURL: (url) => revoked.push(url) };
globalThis.scrollThreadToBottom = () => {};
globalThis.timestampMilliseconds = (value) => Number(value);
globalThis.formatTimestamp = () => "";
globalThis.appendRenderedQuote = () => {};
const images = [];
function node(tag) {
  const value = {
    tag, className: "", children: [], textContent: "", title: "",
    append(...items) { this.children.push(...items); },
    addEventListener() {}, setAttribute() {}, remove() {},
  };
  if (tag === "img") images.push(value);
  return value;
}
globalThis.document = { createElement: node };
globalThis.elements = { messages: { children: [], replaceChildren(...items) { this.children = items; }, append(item) { this.children.push(item); }, scrollTop: 0, scrollHeight: 0, clientHeight: 0 } };
globalThis.window = {};
vm.runInThisContext(reconcile);
state.mediaCache.set("foto.png[0]", { url: "blob:0", width: 640, height: 480 });
// Riga reale confermata SENZA batch_id/batch_index (storica / fallback).
const messages = [{
  id: "real-1", direction: "out", text: "", timestamp: 6,
  attachment: { type: "image/png", media_kind: "image", name: "foto.png", attachment_id: "mirror-0" },
}];
vm.runInThisContext(media);
vm.runInThisContext(render);
renderMessages(messages, "telegram");
assert.equal(messages[0].localPreviewUrl, "blob:0");
assert.equal(state.mediaCache.get("mirror-0").url, "blob:0");
assert.equal(state.optimistic[0].localPreviewUrl, undefined);
assert.equal(images.length, 1);
assert.equal(images[0].src, "blob:0");
""")


@pytest.mark.parametrize(
    ("protocol", "quote_on_all"),
    [("signal", True), ("whatsapp", False), ("telegram", False)],
)
def test_submit_message_multi_attachment_quote_follows_mirror_shape(
    protocol, quote_on_all
):
    """La citazione distribuita sugli optimistic N rispecchia le righe reali:
    Signal la mette su tutte le N righe mirror, WhatsApp/Telegram (manager)
    solo su index 0. Il quotePayload resta nel FormData una sola volta."""
    _run_node(f"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const helper = app.slice(app.indexOf("function mediaKindFromMime("), app.indexOf("\\nfunction clearStagedAttachments"));
const submit = helper + "\\n" + app.slice(app.indexOf("async function submitMessage("), app.indexOf("\\nfunction encodeToken"));
globalThis.state = {{
  sending: 0,
  active: {{ id: "42", protocol: {json.dumps(protocol)} }},
  stagedAttachments: [
    {{ file: new Blob(["a"], {{ type: "image/png" }}), filename: "a.png", previewUrl: null, previewWidth: null, previewHeight: null, attachmentId: "a.png[0]" }},
    {{ file: new Blob(["b"], {{ type: "image/png" }}), filename: "b.png", previewUrl: null, previewWidth: null, previewHeight: null, attachmentId: "b.png[1]" }},
  ],
  replyTo: {{ timestamp: 10, quoteAuthor: "bob", quoteMessage: "Foto", isImage: true, isMedia: true, contentType: "image/jpeg", attachmentId: "att/1", id: "10" }},
  messages: [], optimistic: [], optimisticSequence: 0,
}};
globalThis.elements = {{ messageInput: {{ value: "eco", focus() {{}} }} }};
globalThis.window = {{ SignalTuiReconcile: {{ messageIdentity: (message) => message.id }} }};
globalThis.resizeComposer = () => {{}};
globalThis.updateComposer = () => {{}};
globalThis.renderMessages = () => {{}};
globalThis.cacheMedia = () => {{}};
globalThis.clearStagedAttachments = () => {{ state.stagedAttachments = []; }};
globalThis.showError = assert.fail;
let body;
globalThis.apiFetch = async (_url, options) => {{ body = options.body; return {{ status: 200 }}; }};
globalThis.cancelReply = () => {{ state.replyTo = null; }};
vm.runInThisContext(submit);
(async () => {{
  await submitMessage();
  assert.equal(state.optimistic.length, 2);
  assert.equal(state.optimistic[0].quote_timestamp, 10);
  assert.equal(state.optimistic[0].quote_author, "bob");
  const second = {json.dumps(quote_on_all)} ? 10 : undefined;
  assert.equal(state.optimistic[1].quote_timestamp, second);
  assert.equal(body.get("quote_timestamp"), "10");
  assert.equal(body.get("quote_author"), "bob");
  assert.equal(body.getAll("file").length, 2);
  if ({json.dumps(protocol)} === "signal") assert.equal(body.get("quote_attachment_id"), "att/1");
  else assert.equal(body.get("reply_to_message_id"), "10");
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
""")


def test_static_assets_declare_multi_attachment_ui():
    index = Path("web/static/index.html").read_text(encoding="utf-8")
    assert 'id="attachments-preview"' in index
    assert 'id="attachment-preview"' not in index
    assert 'id="remove-attachment"' not in index
    file_input = re.search(r'<input id="file-input"[^>]*>', index)
    assert file_input is not None
    assert "multiple" in file_input.group(0)

    css = Path("web/static/style.css").read_text(encoding="utf-8")
    assert ".attachments-preview" in css
    assert ".attachment-preview-item" in css

    app = Path("web/static/app.js").read_text(encoding="utf-8")
    assert re.search(r"stagedAttachments\b", app)
    for legacy in (
        r"stagedAttachment\b",
        r"attachmentPreview\b",
        r"removeAttachment\b",
    ):
        assert re.search(legacy, app) is None, legacy

    reconcile = Path("web/static/reconcile.js").read_text(encoding="utf-8")
    assert "multiCandidates" in reconcile
    assert "item.batch_id == null" in reconcile


@pytest.mark.parametrize("protocol", ["signal", "whatsapp", "telegram"])
def test_submit_message_single_attachment_keeps_legacy_optimistic(protocol):
    _run_node(f"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const helper = app.slice(app.indexOf("function mediaKindFromMime("), app.indexOf("\\nfunction clearStagedAttachments"));
const submit = helper + "\\n" + app.slice(app.indexOf("async function submitMessage("), app.indexOf("\\nfunction encodeToken"));
globalThis.state = {{
  sending: 0,
  active: {{ id: "alice", protocol: {json.dumps(protocol)} }},
  stagedAttachments: [{{
    file: new Blob(["x"], {{ type: "image/png" }}), filename: "foto.png",
    previewUrl: "blob:photo", previewWidth: null, previewHeight: null,
    attachmentId: "foto.png[0]",
  }}],
  replyTo: null, messages: [], optimistic: [], optimisticSequence: 0,
}};
globalThis.elements = {{ messageInput: {{ value: "caption", focus() {{}} }} }};
globalThis.window = {{ SignalTuiReconcile: {{ messageIdentity: (message) => message.id }} }};
globalThis.resizeComposer = () => {{}};
globalThis.updateComposer = () => {{}};
globalThis.renderMessages = () => {{}};
globalThis.cacheMedia = () => {{}};
let clearedWithOptions = null;
globalThis.clearStagedAttachments = (options) => {{ clearedWithOptions = options; state.stagedAttachments = []; }};
globalThis.showError = assert.fail;
let request;
globalThis.apiFetch = async (url, options) => {{ request = {{ url, options }}; return {{ status: 200 }}; }};
vm.runInThisContext(submit);
(async () => {{
  await submitMessage();
  assert.equal(state.optimistic.length, 1);
  const optimistic = state.optimistic[0];
  assert.equal(optimistic.batch_id, null);
  assert.equal(optimistic.batch_index, null);
  assert.equal(optimistic.text, "caption");
  assert.equal(optimistic.attachment.attachment_id, "foto.png[0]");
  assert.equal(optimistic.localPreviewUrl, "blob:photo");
  assert.deepEqual(clearedWithOptions, {{ revoke: false }});
  assert.equal(request.options.body.getAll("file").length, 1);
  assert.equal(request.options.body.get("batch_id"), null);
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
""")
