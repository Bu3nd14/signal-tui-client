"use strict";

function messageIdentity(message, index = 0) {
  if (message.id !== null && message.id !== undefined) return String(message.id);
  return `${message.direction}\u0000${message.text}\u0000${message.timestamp}\u0000${index}`;
}

function quoteValue(message, snakeName, camelName) {
  return message[snakeName] ?? message[camelName] ?? null;
}

function messageSignature(message) {
  return JSON.stringify([
    message.direction,
    message.text,
    quoteValue(message, "quote_timestamp", "quoteTimestamp"),
    quoteValue(message, "quote_author", "quoteAuthor"),
    quoteValue(message, "quote_message", "quoteMessage"),
    quoteValue(message, "reply_to_message_id", "replyToMessageId"),
    Boolean(message.attachment),
  ]);
}

function reconcileOptimisticMessages(messages, optimistic, protocol, contactId) {
  const realBySignature = new Map();
  messages.forEach((message, index) => {
    const signature = messageSignature(message);
    const matches = realBySignature.get(signature) || [];
    matches.push(messageIdentity(message, index));
    realBySignature.set(signature, matches);
  });

  const local = optimistic.filter((item) => item.protocol === protocol && item.contactId === contactId);
  const consumed = new Set(local
    .filter((item) => !item.optimistic_id && item.confirmed_message_id)
    .map((item) => item.confirmed_message_id));
  const candidates = local
    .filter((item) => item.optimistic_id && item.optimisticStatus !== "failed")
    .sort((a, b) => b.timestamp - a.timestamp);
  const reconciled = new Map();

  for (const item of candidates) {
    const known = new Set(item.known_message_ids || []);
    const realId = (realBySignature.get(messageSignature(item)) || [])
      .find((id) => !known.has(id) && !consumed.has(id));
    if (realId === undefined) continue;
    const { optimistic_id: ignored, ...confirmed } = item;
    void ignored;
    confirmed.confirmed_message_id = realId;
    reconciled.set(item.optimistic_id, confirmed);
    consumed.add(realId);
  }

  const updated = optimistic.map((item) => reconciled.get(item.optimistic_id) || item);
  const visible = updated.filter((item) =>
    item.protocol === protocol
    && item.contactId === contactId
    && Boolean(item.optimistic_id));
  return { optimistic: updated, visible };
}

const SignalTuiReconcile = {
  messageIdentity,
  reconcileOptimisticMessages,
};

if (typeof window !== "undefined") window.SignalTuiReconcile = SignalTuiReconcile;
if (typeof module !== "undefined") module.exports = SignalTuiReconcile;
