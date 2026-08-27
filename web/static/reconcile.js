"use strict";

function messageIdentity(message, index = 0) {
  if (message.id !== null && message.id !== undefined) return String(message.id);
  return `${message.direction}\u0000${message.text}\u0000${message.timestamp}\u0000${index}`;
}

function quoteValue(message, snakeName, camelName, fallbackName = null) {
  return message[snakeName] ?? message[camelName] ?? (fallbackName ? message[fallbackName] : null) ?? null;
}

function quoteMediaType(message) {
  const explicit = quoteValue(message, "quote_media_type", "quoteMediaType");
  if (explicit) return String(explicit).toLowerCase();
  const text = quoteValue(message, "quote_message", "quoteMessage", "quote_text");
  if (typeof text !== "string") return null;
  const placeholders = [
    ["🖼️ Immagine", "image"],
    ["🎬 Video", "video"],
    ["🎵 Audio", "audio"],
    ["🎨 Sticker", "sticker"],
    ["📎 File", "attachment"],
  ];
  const match = placeholders.find(([placeholder]) => text === placeholder || text.endsWith(` — ${placeholder}`));
  if (match) return match[1];
  return null;
}

function quoteSignatureValue(message) {
  const mediaType = quoteMediaType(message);
  return mediaType ? `media:${mediaType}` : quoteValue(message, "quote_message", "quoteMessage", "quote_text");
}

const MEDIA_PLACEHOLDERS = new Map([
  ["🖼️ Immagine", "image"],
  ["🎬 Video", "video"],
  ["🎵 Audio", "audio"],
  ["🎨 Sticker", "sticker"],
  ["📎 File", "attachment"],
]);

function isTemporaryUploadName(value) {
  return typeof value === "string" && /(?:^|[\\/])upload-[^\\/]+$/i.test(value.trim());
}

function messageMediaType(message) {
  if (!message.attachment) return null;
  const explicit = message.msg_type ?? message.msgType;
  if (explicit && explicit !== "text") return String(explicit).toLowerCase();
  const mimeType = message.attachment.type;
  if (typeof mimeType === "string") {
    const category = mimeType.toLowerCase().split("/", 1)[0];
    if (["image", "video", "audio"].includes(category)) return category;
  }
  return "attachment";
}

function signatureText(message) {
  const mediaType = messageMediaType(message);
  const text = message.text ?? "";
  if (mediaType && (!text || MEDIA_PLACEHOLDERS.has(text) || isTemporaryUploadName(text))) {
    return `media:${mediaType}`;
  }
  return text;
}

function messageDisplayText(message) {
  return message.attachment && isTemporaryUploadName(message.text) ? "" : (message.text || "");
}

function messageSignature(message) {
  return JSON.stringify([
    message.direction,
    signatureText(message),
    quoteValue(message, "quote_timestamp", "quoteTimestamp"),
    quoteValue(message, "quote_author", "quoteAuthor"),
    quoteSignatureValue(message),
    quoteValue(message, "reply_to_message_id", "replyToMessageId"),
    messageMediaType(message),
  ]);
}

function messageLooseSignature(message) {
  return JSON.stringify([
    message.direction,
    signatureText(message),
    quoteSignatureValue(message),
    messageMediaType(message),
  ]);
}

function messageQuoteAgnosticSignature(message) {
  return JSON.stringify([
    message.direction,
    signatureText(message),
    messageMediaType(message),
  ]);
}

function replyQuoteMessage(message) {
  const displayText = messageDisplayText(message);
  if (displayText) return displayText;
  if (!message.attachment) return "";
  const attachmentId = message.attachment.attachment_id || "";
  return (isTemporaryUploadName(message.attachment.name) ? "" : message.attachment.name)
    || attachmentId.split("?", 1)[0].split("/").filter(Boolean).pop()
    || "Allegato";
}

function reconcileOptimisticMessages(messages, optimistic, protocol, contactId) {
  const realBySignature = new Map();
  const realByLooseSignature = new Map();
  const quoteLessWhatsAppEchoes = new Map();
  messages.forEach((message, index) => {
    const identity = messageIdentity(message, index);
    const signature = messageSignature(message);
    const matches = realBySignature.get(signature) || [];
    matches.push(identity);
    realBySignature.set(signature, matches);
    const looseSignature = messageLooseSignature(message);
    const looseMatches = realByLooseSignature.get(looseSignature) || [];
    looseMatches.push(identity);
    realByLooseSignature.set(looseSignature, looseMatches);
    if (protocol === "whatsapp" && quoteSignatureValue(message) == null) {
      const quoteAgnosticSignature = messageQuoteAgnosticSignature(message);
      const quoteLessMatches = quoteLessWhatsAppEchoes.get(quoteAgnosticSignature) || [];
      quoteLessMatches.push(identity);
      quoteLessWhatsAppEchoes.set(quoteAgnosticSignature, quoteLessMatches);
    }
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
    const exactMatches = realBySignature.get(messageSignature(item)) || [];
    const looseMatches = realByLooseSignature.get(messageLooseSignature(item)) || [];
    const quoteLessMatches = protocol === "whatsapp" && quoteSignatureValue(item) != null
      ? quoteLessWhatsAppEchoes.get(messageQuoteAgnosticSignature(item)) || []
      : [];
    const available = (id) => !known.has(id) && !consumed.has(id);
    const realId = exactMatches.find(available)
      ?? looseMatches.find(available)
      ?? quoteLessMatches.find(available);
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
  messageDisplayText,
  reconcileOptimisticMessages,
  replyQuoteMessage,
};

if (typeof window !== "undefined") window.SignalTuiReconcile = SignalTuiReconcile;
if (typeof module !== "undefined") module.exports = SignalTuiReconcile;
