from __future__ import annotations

import subprocess


def _run_node(source: str) -> None:
    completed = subprocess.run(
        ["node", "-e", source], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def test_contact_search_dedup_collapses_lid_and_c_us_by_phone():
    _run_node(r"""
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const app = fs.readFileSync("./web/static/app.js", "utf8");
const start = app.indexOf("const contactDedupKey =");
const end = app.indexOf("if (elements.contactSearch)", start);
vm.runInThisContext(app.slice(start, end));

const lidChat = {
  id: "139153@lid",
  protocol: "whatsapp",
  extras: { lid: "139153@lid", phone: "393331234567" },
};
const cUsBook = {
  id: "393331234567@c.us",
  protocol: "whatsapp",
  extras: { phone: "393331234567" },
};
const otherChat = {
  id: "999@lid",
  protocol: "whatsapp",
  extras: { lid: "999@lid", phone: "393339999999" },
};

assert.equal(contactDedupKey(lidChat), "whatsapp:393331234567");
assert.equal(contactDedupKey(cUsBook), "whatsapp:393331234567");
assert.equal(contactDedupKey(otherChat), "whatsapp:393339999999");

assert.deepEqual(dedupeContacts([lidChat, cUsBook, otherChat]), [
  lidChat,
  otherChat,
]);

const seen = new Set(dedupeContacts([lidChat, otherChat]).map(contactDedupKey));
const merged = [
  lidChat,
  otherChat,
  ...[cUsBook].filter((contact) => {
    const key = contactDedupKey(contact);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  }),
];
assert.deepEqual(merged, [lidChat, otherChat]);

const phoneOnly = { id: "393331234567@c.us", protocol: "whatsapp", extras: {} };
assert.equal(contactDedupKey(phoneOnly), "whatsapp:393331234567@c.us");
""")
