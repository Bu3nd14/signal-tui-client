"use strict";

const TOKEN_KEY = "signal-tui-web-token";
const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  contacts: [],
  protocolFilter: "all",
  active: null,
  socket: null,
  reconnectTimer: null,
  reconnectAttempt: 0,
  messageRequest: null,
  mediaRequests: new Set(),
  objectUrls: new Set(),
  messages: [],
  optimistic: [],
  optimisticSequence: 0,
  sending: false,
  stagedAttachment: null,
  replyTo: null,
  emojiData: null,
  emojiRequest: null,
  emojiFailed: false,
  emojiCategory: 0,
};

const elements = {
  app: document.querySelector("#app"),
  contacts: document.querySelector("#contact-list"),
  protocolTabs: document.querySelector("#protocol-tabs"),
  contactStatus: document.querySelector("#contact-status"),
  messages: document.querySelector("#message-list"),
  threadName: document.querySelector("#thread-name"),
  threadMeta: document.querySelector("#thread-meta"),
  connection: document.querySelector("#connection-state"),
  errorBanner: document.querySelector("#error-banner"),
  errorText: document.querySelector("#error-text"),
  tokenDialog: document.querySelector("#token-dialog"),
  tokenInput: document.querySelector("#token-input"),
  tokenError: document.querySelector("#token-error"),
  cancelToken: document.querySelector("#cancel-token"),
  composer: document.querySelector("#composer"),
  composerShell: document.querySelector("#composer-shell"),
  messageInput: document.querySelector("#message-input"),
  sendMessage: document.querySelector("#send-message"),
  sendIcon: document.querySelector(".send-icon"),
  sendSpinner: document.querySelector(".send-spinner"),
  attachmentPreview: document.querySelector("#attachment-preview"),
  attachmentPreviewImage: document.querySelector("#attachment-preview-image"),
  attachmentPreviewName: document.querySelector("#attachment-preview-name"),
  removeAttachment: document.querySelector("#remove-attachment"),
  replyBanner: document.querySelector("#reply-banner"),
  replyAuthor: document.querySelector("#reply-author"),
  replySnippet: document.querySelector("#reply-snippet"),
  cancelReply: document.querySelector("#cancel-reply"),
  emojiToggle: document.querySelector("#emoji-toggle"),
  emojiPicker: document.querySelector("#emoji-picker"),
  emojiTabs: document.querySelector("#emoji-tabs"),
  emojiSearch: document.querySelector("#emoji-search"),
  emojiGrid: document.querySelector("#emoji-grid"),
};

function showError(message) {
  elements.errorText.textContent = message;
  elements.errorBanner.hidden = false;
}

function scrollThreadToBottom() {
  elements.messages.scrollTop = elements.messages.scrollHeight;
}

function requestToken(invalid = false) {
  closeEmojiPicker({ focus: false });
  elements.tokenError.hidden = !invalid;
  elements.tokenInput.value = state.token;
  elements.cancelToken.hidden = !state.token;
  if (!elements.tokenDialog.open) elements.tokenDialog.showModal();
  window.setTimeout(() => elements.tokenInput.focus(), 0);
}

function handleUnauthorized() {
  disconnectSocket();
  requestToken(true);
}

async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body instanceof FormData) headers.delete("Content-Type");
  headers.set("Authorization", `Bearer ${state.token}`);
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    handleUnauthorized();
    throw new Error("unauthorized");
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response;
}

function formatTimestamp(value, includeDate = true) {
  if (value === null || value === undefined || value === "") return "";
  const numeric = Number(value);
  const date = Number.isFinite(numeric)
    ? new Date(numeric < 100000000000 ? numeric * 1000 : numeric)
    : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, includeDate
    ? { dateStyle: "medium", timeStyle: "short" }
    : { hour: "2-digit", minute: "2-digit" }).format(date);
}

function timestampMilliseconds(value) {
  const numeric = Number(value);
  return numeric < 100000000000 ? numeric * 1000 : numeric;
}

function contactInitial(contact) {
  return (contact.display_name || contact.id || "?").trim().charAt(0).toUpperCase();
}

function protocolIcon(protocol, size = 15) {
  const icons = {
    signal: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 0q-.934 0-1.83.139l.17 1.111a11 11 0 0 1 3.32 0l.172-1.111A12 12 0 0 0 12 0M9.152.34A12 12 0 0 0 5.77 1.742l.584.961a10.8 10.8 0 0 1 3.066-1.27zm5.696 0-.268 1.094a10.8 10.8 0 0 1 3.066 1.27l.584-.962A12 12 0 0 0 14.848.34M12 2.25a9.75 9.75 0 0 0-8.539 14.459c.074.134.1.292.064.441l-1.013 4.338 4.338-1.013a.62.62 0 0 1 .441.064A9.7 9.7 0 0 0 12 21.75c5.385 0 9.75-4.365 9.75-9.75S17.385 2.25 12 2.25m-7.092.068a12 12 0 0 0-2.59 2.59l.909.664a11 11 0 0 1 2.345-2.345zm14.184 0-.664.909a11 11 0 0 1 2.345 2.345l.909-.664a12 12 0 0 0-2.59-2.59M1.742 5.77A12 12 0 0 0 .34 9.152l1.094.268a10.8 10.8 0 0 1 1.269-3.066zm20.516 0-.961.584a10.8 10.8 0 0 1 1.27 3.066l1.093-.268a12 12 0 0 0-1.402-3.383M.138 10.168A12 12 0 0 0 0 12q0 .934.139 1.83l1.111-.17A11 11 0 0 1 1.125 12q0-.848.125-1.66zm23.723.002-1.111.17q.125.812.125 1.66c0 .848-.042 1.12-.125 1.66l1.111.172a12.1 12.1 0 0 0 0-3.662M1.434 14.58l-1.094.268a12 12 0 0 0 .96 2.591l-.265 1.14 1.096.255.36-1.539-.188-.365a10.8 10.8 0 0 1-.87-2.35m21.133 0a10.8 10.8 0 0 1-1.27 3.067l.962.584a12 12 0 0 0 1.402-3.383zm-1.793 3.848a11 11 0 0 1-2.345 2.345l.664.909a12 12 0 0 0 2.59-2.59zm-19.959 1.1L.357 21.48a1.8 1.8 0 0 0 2.162 2.161l1.954-.455-.256-1.095-1.953.455a.675.675 0 0 1-.81-.81l.454-1.954zm16.832 1.769a10.8 10.8 0 0 1-3.066 1.27l.268 1.093a12 12 0 0 0 3.382-1.402zm-10.94.213-1.54.36.256 1.095 1.139-.266c.814.415 1.683.74 2.591.961l.268-1.094a10.8 10.8 0 0 1-2.35-.869zm3.634 1.24-.172 1.111a12.1 12.1 0 0 0 3.662 0l-.17-1.111q-.812.125-1.66.125a11 11 0 0 1-1.66-.125"/></svg>`,
    whatsapp: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z"/></svg>`,
    telegram: `<svg width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z"/></svg>`,
  };
  return icons[protocol] || "";
}

function renderContacts() {
  elements.contacts.replaceChildren();
  const sortedContacts = [...state.contacts].sort((a, b) => Number(b.last_message_ts || 0) - Number(a.last_message_ts || 0));
  const contacts = state.protocolFilter === "all"
    ? sortedContacts
    : sortedContacts.filter((contact) => contact.protocol === state.protocolFilter);
  const activeMatchesFilter = state.protocolFilter === "all" || state.active?.protocol === state.protocolFilter;
  if (state.active && activeMatchesFilter && !contacts.some((contact) => contact.id === state.active.id && contact.protocol === state.active.protocol)) {
    contacts.unshift(state.active);
  }
  for (const tab of elements.protocolTabs.querySelectorAll("[data-protocol]")) {
    const active = tab.dataset.protocol === state.protocolFilter;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  elements.contactStatus.textContent = contacts.length ? "" : "Nessuna conversazione disponibile.";
  for (const contact of contacts) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "contact";
    if (state.active?.id === contact.id && state.active?.protocol === contact.protocol) button.classList.add("active");

    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.textContent = contactInitial(contact);
    const copy = document.createElement("span");
    copy.className = "contact-copy";
    const main = document.createElement("span");
    main.className = "contact-main";
    const name = document.createElement("span");
    name.className = "contact-name";
    name.textContent = contact.display_name || contact.id;
    const icon = document.createElement("span");
    icon.className = "protocol-icon";
    icon.title = contact.protocol;
    icon.innerHTML = protocolIcon(contact.protocol);
    main.append(name);
    copy.append(main);
    button.append(avatar, copy);
    if (Number(contact.unread) > 0) {
      const unread = document.createElement("span");
      unread.className = "unread-badge";
      unread.textContent = Number(contact.unread) > 99 ? "99+" : String(contact.unread);
      button.append(unread);
    }
    button.append(icon);
    button.addEventListener("click", () => openThread(contact));
    elements.contacts.append(button);
  }
}

async function loadContacts({ quiet = false } = {}) {
  if (!state.token) return requestToken();
  if (!quiet) elements.contactStatus.textContent = "Caricamento…";
  try {
    const response = await apiFetch("/api/contacts");
    state.contacts = await response.json();
    if (state.active) {
      const current = state.contacts.find((contact) => contact.id === state.active.id && contact.protocol === state.active.protocol);
      if (current) state.active = current;
    }
    renderContacts();
  } catch (error) {
    if (error.message !== "unauthorized") {
      elements.contactStatus.textContent = "Impossibile caricare le conversazioni.";
      showError("Errore di rete durante il caricamento delle conversazioni.");
    }
  }
}

function clearMedia() {
  for (const controller of state.mediaRequests) controller.abort();
  state.mediaRequests.clear();
  for (const url of state.objectUrls) URL.revokeObjectURL(url);
  state.objectUrls.clear();
}

async function loadImage(container, image, path) {
  const controller = new AbortController();
  state.mediaRequests.add(controller);
  try {
    const response = await apiFetch(path, { signal: controller.signal });
    const url = URL.createObjectURL(await response.blob());
    state.objectUrls.add(url);
    image.addEventListener("load", () => {
      container.querySelector(".attachment-loading")?.remove();
      scrollThreadToBottom();
    }, { once: true });
    image.src = url;
  } catch (error) {
    if (error.name !== "AbortError") {
      container.replaceChildren();
      const fallback = document.createElement("div");
      fallback.className = "attachment-error";
      fallback.textContent = "▧  Immagine non disponibile";
      container.append(fallback);
    }
  } finally {
    state.mediaRequests.delete(controller);
  }
}

function imageAttachment(attachment, protocol) {
  const container = document.createElement("div");
  container.className = "attachment";
  const loading = document.createElement("div");
  loading.className = "attachment-loading";
  const spinner = document.createElement("span");
  spinner.className = "spinner";
  spinner.setAttribute("aria-label", "Caricamento immagine");
  loading.append(spinner);
  const image = document.createElement("img");
  image.alt = attachment.name || "Immagine allegata";
  container.append(loading, image);
  const path = `/api/media/${encodeURIComponent(protocol)}/${attachment.attachment_id.split("/").map(encodeURIComponent).join("/")}`;
  loadImage(container, image, path);
  return container;
}

function attachmentName(item) {
  const attachmentId = item.attachment?.attachment_id || "";
  return item.attachment?.name || attachmentId.split("?", 1)[0].split("/").filter(Boolean).pop() || "Allegato";
}

function replyAuthor(item) {
  return item.direction === "out" ? "Tu" : (state.active?.display_name || state.active?.id || "Contatto");
}

function updateReplyBanner() {
  const reply = state.replyTo;
  elements.replyBanner.hidden = !reply;
  if (!reply) {
    elements.replyAuthor.textContent = "";
    elements.replySnippet.textContent = "";
    elements.messageInput.placeholder = "Scrivi un messaggio";
    return;
  }
  elements.replyAuthor.textContent = reply.author;
  const imageMark = reply.isImage ? "🖼️ " : "";
  const snippet = reply.quoteMessage || "Messaggio";
  elements.replySnippet.textContent = `${imageMark}${snippet.length > 80 ? `${snippet.slice(0, 79)}…` : snippet}`;
  elements.messageInput.placeholder = `Rispondendo a ${reply.author}`;
}

function cancelReply() {
  state.replyTo = null;
  updateReplyBanner();
}

function startReply(item) {
  if (!state.active || item.optimistic_id) return;
  const timestamp = timestampMilliseconds(item.timestamp);
  const id = item.id === null || item.id === undefined ? null : String(item.id);
  if (!Number.isFinite(timestamp)) return;
  if (state.active.protocol === "telegram" && (!id || !/^\d+$/.test(id) || Number(id) <= 0)) return;
  if (state.active.protocol === "whatsapp" && !id) return;
  state.replyTo = {
    id,
    timestamp,
    author: replyAuthor(item),
    quoteAuthor: state.active.id,
    quoteMessage: window.SignalTuiReconcile.replyQuoteMessage(item),
    isMedia: Boolean(item.attachment),
    isImage: Boolean(item.attachment?.type?.toLowerCase().startsWith("image/")),
    contentType: item.attachment?.type,
    attachmentId: item.attachment?.attachment_id,
  };
  updateReplyBanner();
  elements.messageInput.focus();
}

function appendRenderedQuote(bubble, item) {
  const quoteText = item.quote_text ?? item.quote_message;
  if (quoteText == null && item.quote_timestamp == null && item.quote_author == null) return;
  const quote = document.createElement("div");
  quote.className = "message-quote";
  const author = document.createElement("strong");
  author.textContent = item.quote_author || "Messaggio citato";
  const text = document.createElement("span");
  text.textContent = quoteText || "Messaggio";
  quote.append(author, text);
  bubble.append(quote);
}

function renderMessages(messages, protocol) {
  clearMedia();
  elements.messages.replaceChildren();
  const active = state.active;
  const reconciliation = window.SignalTuiReconcile.reconcileOptimisticMessages(
    messages,
    state.optimistic,
    active?.protocol,
    active?.id,
  );
  state.optimistic = reconciliation.optimistic;
  for (const item of state.optimistic) {
    if (item.localPreviewUrl && !item.optimistic_id) {
      URL.revokeObjectURL(item.localPreviewUrl);
      delete item.localPreviewUrl;
    }
  }
  const displayed = [...messages, ...reconciliation.visible].sort((a, b) => timestampMilliseconds(a.timestamp) - timestampMilliseconds(b.timestamp));
  if (!displayed.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Nessun messaggio archiviato in questa conversazione.";
    elements.messages.append(empty);
    scrollThreadToBottom();
    requestAnimationFrame(scrollThreadToBottom);
    return;
  }
  for (const item of displayed) {
    const message = document.createElement("article");
    message.className = `message ${item.direction === "out" ? "out" : "in"}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    appendRenderedQuote(bubble, item);
    const isImage = item.attachment?.type?.toLowerCase().startsWith("image/");
    if (isImage) {
      if (item.localPreviewUrl) {
        const preview = document.createElement("div");
        preview.className = "attachment local-preview";
        const image = document.createElement("img");
        image.src = item.localPreviewUrl;
        image.alt = item.attachment.name || "Immagine allegata";
        preview.append(image);
        bubble.append(preview);
      } else {
        bubble.append(imageAttachment(item.attachment, protocol));
      }
    }
    const safeText = window.SignalTuiReconcile.messageDisplayText(item);
    const displayText = safeText || (item.attachment && !isImage ? attachmentName(item) : "");
    if (displayText) {
      const text = document.createElement("div");
      text.className = "message-text";
      text.textContent = displayText;
      bubble.append(text);
    }
    const time = document.createElement("time");
    time.className = "message-time";
    time.textContent = formatTimestamp(item.timestamp);
    if (item.optimisticStatus) {
      const status = document.createElement("span");
      status.className = `message-status ${item.optimisticStatus}`;
      status.textContent = item.optimisticStatus === "failed" ? " · fallito" : item.optimisticStatus === "sent" ? " · inviato" : " · invio…";
      time.append(status);
    }
    bubble.append(time);
    message.append(bubble);
    if (!item.optimistic_id) {
      const reply = document.createElement("button");
      reply.type = "button";
      reply.className = "message-reply";
      reply.textContent = "↩";
      reply.setAttribute("aria-label", "Rispondi al messaggio");
      reply.title = "Rispondi";
      reply.addEventListener("click", () => startReply(item));
      message.append(reply);
    }
    elements.messages.append(message);
  }
  scrollThreadToBottom();
  requestAnimationFrame(scrollThreadToBottom);
}

async function loadMessages() {
  const active = state.active;
  if (!active) return;
  state.messageRequest?.abort();
  const controller = new AbortController();
  state.messageRequest = controller;
  try {
    const query = new URLSearchParams({ proto: active.protocol, contact_id: active.id });
    const response = await apiFetch(`/api/messages?${query}`, { signal: controller.signal });
    const messages = await response.json();
    if (state.active?.id === active.id && state.active?.protocol === active.protocol) {
      state.messages = messages;
      renderMessages(messages, active.protocol);
      scrollThreadToBottom();
    }
  } catch (error) {
    if (error.name !== "AbortError" && error.message !== "unauthorized") showError("Errore di rete durante il caricamento dei messaggi.");
  } finally {
    if (state.messageRequest === controller) state.messageRequest = null;
  }
}

function openThread(contact) {
  closeEmojiPicker({ focus: false });
  cancelReply();
  state.active = contact;
  elements.threadName.textContent = contact.display_name || contact.id;
  elements.threadMeta.innerHTML = `${protocolIcon(contact.protocol, 13)}<span class="thread-proto-name">${contact.protocol}</span>`;
  elements.app.classList.add("thread-open");
  elements.composerShell.hidden = false;
  state.messages = [];
  renderContacts();
  elements.messages.replaceChildren();
  const loading = document.createElement("div");
  loading.className = "empty-state";
  loading.textContent = "Caricamento messaggi…";
  elements.messages.append(loading);
  loadMessages();
}

function normalizeEmojiSearch(value) {
  return value.toLocaleLowerCase().replaceAll("_", " ").replaceAll("-", " ").trim();
}

async function loadEmojiData() {
  if (state.emojiData) return state.emojiData;
  if (state.emojiFailed) throw new Error("emoji unavailable");
  if (!state.emojiRequest) {
    state.emojiRequest = apiFetch("/api/emoji")
      .then((response) => response.json())
      .then((categories) => {
        if (!Array.isArray(categories) || !categories.length) throw new Error("invalid emoji data");
        state.emojiData = categories;
        return categories;
      })
      .catch((error) => {
        state.emojiFailed = true;
        throw error;
      })
      .finally(() => { state.emojiRequest = null; });
  }
  return state.emojiRequest;
}

function emojiCells() {
  return [...elements.emojiGrid.querySelectorAll(".emoji-cell")];
}

function setEmojiRovingIndex(index, { focus = false } = {}) {
  const cells = emojiCells();
  if (!cells.length) return;
  const bounded = Math.max(0, Math.min(index, cells.length - 1));
  cells.forEach((cell, cellIndex) => { cell.tabIndex = cellIndex === bounded ? 0 : -1; });
  if (focus) cells[bounded].focus();
}

function insertEmoji(char) {
  const input = elements.messageInput;
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? start;
  input.setRangeText(char, start, end, "end");
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.focus();
}

function renderEmojiGrid() {
  elements.emojiGrid.replaceChildren();
  const query = normalizeEmojiSearch(elements.emojiSearch.value);
  const categories = state.emojiData || [];
  const activeCategory = categories[state.emojiCategory];
  const matches = [];
  const candidates = query ? categories : (activeCategory ? [activeCategory] : []);
  for (const category of candidates) {
    const categoryMatches = normalizeEmojiSearch(category.category).includes(query);
    for (const char of category.emojis) {
      const alias = category.aliases?.[char] || "";
      if (!query || categoryMatches || normalizeEmojiSearch(alias).includes(query)) {
        matches.push({ char, alias, category: category.category });
        if (query && matches.length >= 60) break;
      }
    }
    if (query && matches.length >= 60) break;
  }
  if (!matches.length) {
    const empty = document.createElement("p");
    empty.className = "emoji-empty";
    empty.textContent = "Nessuna emoji trovata";
    elements.emojiGrid.append(empty);
    return;
  }
  for (const [index, item] of matches.entries()) {
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "emoji-cell";
    cell.tabIndex = index === 0 ? 0 : -1;
    cell.setAttribute("role", "gridcell");
    cell.setAttribute("aria-label", item.alias || `${item.category}: ${item.char}`);
    cell.title = item.alias || item.category;
    cell.textContent = item.char;
    cell.addEventListener("click", () => insertEmoji(item.char));
    elements.emojiGrid.append(cell);
  }
}

function renderEmojiTabs() {
  elements.emojiTabs.replaceChildren();
  for (const [index, category] of state.emojiData.entries()) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = `emoji-tab${index === state.emojiCategory ? " active" : ""}`;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(index === state.emojiCategory));
    tab.setAttribute("aria-label", category.category);
    tab.title = category.category;
    tab.textContent = category.icon;
    tab.addEventListener("click", () => {
      state.emojiCategory = index;
      elements.emojiSearch.value = "";
      renderEmojiTabs();
      renderEmojiGrid();
    });
    elements.emojiTabs.append(tab);
  }
}

function closeEmojiPicker({ focus = true } = {}) {
  if (elements.emojiPicker.hidden) return;
  elements.emojiPicker.hidden = true;
  elements.emojiToggle.setAttribute("aria-expanded", "false");
  elements.emojiToggle.setAttribute("aria-label", "Apri selettore emoji");
  if (focus) elements.messageInput.focus();
}

async function toggleEmojiPicker() {
  if (!elements.emojiPicker.hidden) {
    closeEmojiPicker();
    return;
  }
  try {
    await loadEmojiData();
  } catch (error) {
    if (error.message !== "unauthorized") showError("Impossibile caricare il selettore emoji.");
    return;
  }
  renderEmojiTabs();
  renderEmojiGrid();
  if (elements.tokenDialog.open) elements.tokenDialog.close();
  elements.emojiPicker.hidden = false;
  elements.emojiToggle.setAttribute("aria-expanded", "true");
  elements.emojiToggle.setAttribute("aria-label", "Chiudi selettore emoji");
  elements.emojiSearch.focus();
}

function resizeComposer() {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 160)}px`;
}

function updateComposer() {
  elements.sendMessage.disabled = state.sending || (!elements.messageInput.value.trim() && !state.stagedAttachment);
  elements.messageInput.disabled = state.sending;
  elements.removeAttachment.disabled = state.sending;
  elements.cancelReply.disabled = state.sending;
  elements.sendIcon.hidden = state.sending;
  elements.sendSpinner.hidden = !state.sending;
}

function clearStagedAttachment({ revoke = true } = {}) {
  if (state.stagedAttachment && revoke) URL.revokeObjectURL(state.stagedAttachment.previewUrl);
  state.stagedAttachment = null;
  elements.attachmentPreview.hidden = true;
  elements.attachmentPreviewImage.removeAttribute("src");
  elements.attachmentPreviewName.textContent = "";
  updateComposer();
}

function stageAttachment(file) {
  if (!file || !file.type.startsWith("image/") || state.sending) return;
  if (file.size > 20 * 1024 * 1024) {
    showError("L'immagine supera il limite di 20 MiB.");
    return;
  }
  clearStagedAttachment();
  const extensions = { "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp" };
  const extension = extensions[file.type];
  if (!extension) {
    showError("Formato immagine non supportato.");
    return;
  }
  const filename = `clipboard-${Date.now()}.${extension}`;
  const previewUrl = URL.createObjectURL(file);
  state.stagedAttachment = { file, filename, previewUrl };
  elements.attachmentPreviewImage.src = previewUrl;
  elements.attachmentPreviewName.textContent = filename;
  elements.attachmentPreview.hidden = false;
  updateComposer();
}

async function submitMessage() {
  if (state.sending || !state.active) return;
  const text = elements.messageInput.value;
  const attachment = state.stagedAttachment;
  const reply = state.replyTo ? { ...state.replyTo } : null;
  if (!text.trim() && !attachment) return;
  const active = { ...state.active };
  const timestamp = Date.now();
  const optimistic = {
    optimistic_id: `${timestamp}-${++state.optimisticSequence}`,
    protocol: active.protocol,
    contactId: active.id,
    text: attachment ? "" : text,
    direction: "out",
    timestamp,
    optimisticStatus: "sending",
    known_message_ids: state.messages.map(window.SignalTuiReconcile.messageIdentity),
  };
  if (reply) {
    optimistic.quote_timestamp = reply.timestamp;
    optimistic.quote_author = reply.quoteAuthor;
    optimistic.quote_message = reply.quoteMessage;
    optimistic.quote_text = reply.quoteMessage;
    if (reply.isImage && active.protocol !== "signal") optimistic.quote_media_type = "image";
  }
  if (attachment) {
    optimistic.attachment = { type: attachment.file.type, name: attachment.filename, attachment_id: attachment.filename };
    optimistic.localPreviewUrl = attachment.previewUrl;
  }
  state.optimistic.push(optimistic);
  state.sending = true;
  elements.messageInput.value = "";
  if (attachment) clearStagedAttachment({ revoke: false });
  resizeComposer();
  updateComposer();
  if (state.active?.id === active.id && state.active?.protocol === active.protocol) renderMessages(state.messages, active.protocol);
  try {
    const quotePayload = reply ? {
      quote_timestamp: reply.timestamp,
      quote_author: reply.quoteAuthor,
      quote_message: active.protocol === "signal" && reply.isMedia ? "" : reply.quoteMessage,
      ...(active.protocol === "signal"
        ? {
          ...(reply.contentType ? { quote_content_type: reply.contentType } : {}),
          ...(reply.attachmentId ? { quote_attachment_id: reply.attachmentId } : {}),
        }
        : { reply_to_message_id: reply.id }),
    } : {};
    if (attachment) {
      const body = new FormData();
      body.set("protocol", active.protocol);
      body.set("contact_id", active.id);
      body.set("text", text);
      for (const [key, value] of Object.entries(quotePayload)) body.set(key, String(value));
      body.set("file", attachment.file, attachment.filename);
      await apiFetch("/api/send", { method: "POST", body });
    } else {
      await apiFetch("/api/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ protocol: active.protocol, contact_id: active.id, text, ...quotePayload }),
      });
    }
    optimistic.optimisticStatus = "sent";
    if (state.replyTo && reply && state.replyTo.timestamp === reply.timestamp) cancelReply();
  } catch (error) {
    optimistic.optimisticStatus = "failed";
    if (error.message !== "unauthorized") showError("Impossibile inviare il messaggio.");
  } finally {
    state.sending = false;
    updateComposer();
    if (state.active?.id === active.id && state.active?.protocol === active.protocol) renderMessages(state.messages, active.protocol);
    elements.messageInput.focus();
  }
}

function encodeToken(token) {
  const bytes = new TextEncoder().encode(token);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function disconnectSocket() {
  window.clearTimeout(state.reconnectTimer);
  state.reconnectTimer = null;
  if (state.socket) {
    state.socket.onclose = null;
    state.socket.close();
    state.socket = null;
  }
  elements.connection.className = "connection-state offline";
  elements.connection.textContent = "offline";
}

function scheduleReconnect() {
  if (!state.token || state.reconnectTimer) return;
  const delay = Math.min(30000, 1000 * (2 ** state.reconnectAttempt));
  state.reconnectAttempt += 1;
  state.reconnectTimer = window.setTimeout(() => {
    state.reconnectTimer = null;
    connectSocket();
  }, delay);
}

function connectSocket() {
  disconnectSocket();
  if (!state.token) return;
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const protocol = `signal-tui-token.${encodeToken(state.token)}`;
  const socket = new WebSocket(`${scheme}//${location.host}/ws`, ["signal-tui-bearer", protocol]);
  state.socket = socket;
  elements.connection.textContent = "connessione…";
  socket.onopen = () => {
    state.reconnectAttempt = 0;
    elements.connection.className = "connection-state online";
    elements.connection.textContent = "live";
  };
  socket.onmessage = (event) => {
    try {
      const update = JSON.parse(event.data);
      if (update.type !== "message" || !update.payload) return;
      loadContacts({ quiet: true });
      if (state.active?.id === String(update.payload.contact_id) && state.active?.protocol === update.payload.protocol) loadMessages();
    } catch {
      showError("Aggiornamento live non valido ricevuto dal server.");
    }
  };
  socket.onerror = () => socket.close();
  socket.onclose = (event) => {
    if (state.socket !== socket) return;
    state.socket = null;
    elements.connection.className = "connection-state offline";
    elements.connection.textContent = "offline";
    if (event.code === 1008) requestToken(true);
    else scheduleReconnect();
  };
}

document.querySelector("#refresh-contacts").addEventListener("click", () => loadContacts());
document.querySelector("#open-token").addEventListener("click", () => requestToken());
document.querySelector("#back-button").addEventListener("click", () => elements.app.classList.remove("thread-open"));
document.querySelector("#dismiss-error").addEventListener("click", () => { elements.errorBanner.hidden = true; });
elements.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  submitMessage();
});
elements.messageInput.addEventListener("input", () => {
  resizeComposer();
  updateComposer();
});
elements.messageInput.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey) return;
  event.preventDefault();
  if (!state.sending) submitMessage();
});
elements.emojiToggle.addEventListener("click", toggleEmojiPicker);
elements.emojiSearch.addEventListener("input", renderEmojiGrid);
elements.emojiSearch.addEventListener("keydown", (event) => {
  if (event.key !== "ArrowDown") return;
  const cells = emojiCells();
  if (!cells.length) return;
  event.preventDefault();
  setEmojiRovingIndex(0, { focus: true });
});
elements.emojiGrid.addEventListener("keydown", (event) => {
  const cell = event.target.closest(".emoji-cell");
  if (!cell) return;
  const cells = emojiCells();
  const index = cells.indexOf(cell);
  const offsets = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -8, ArrowDown: 8 };
  if (Object.hasOwn(offsets, event.key)) {
    event.preventDefault();
    setEmojiRovingIndex(index + offsets[event.key], { focus: true });
  } else if (event.key === "Enter") {
    event.preventDefault();
    cell.click();
  }
});
elements.emojiPicker.addEventListener("keydown", (event) => {
  const searchShortcut = event.key === "/" && event.target !== elements.emojiSearch;
  const findShortcut = event.ctrlKey && event.key.toLocaleLowerCase() === "f";
  if (searchShortcut || findShortcut) {
    event.preventDefault();
    elements.emojiSearch.focus();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || elements.emojiPicker.hidden) return;
  event.preventDefault();
  closeEmojiPicker();
});
elements.composer.addEventListener("paste", (event) => {
  const item = [...(event.clipboardData?.items || [])]
    .find((candidate) => candidate.type.startsWith("image/"));
  if (!item) return;
  event.preventDefault();
  stageAttachment(item.getAsFile());
});
elements.removeAttachment.addEventListener("click", () => clearStagedAttachment());
elements.cancelReply.addEventListener("click", cancelReply);
if (elements.protocolTabs) {
  elements.protocolTabs.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-protocol]");
    if (!tab) return;
    const protocol = tab.dataset.protocol;
    state.protocolFilter = protocol;
    if (protocol !== "all" && state.active?.protocol !== protocol) {
      const firstContact = state.contacts.find((contact) => contact.protocol === protocol);
      if (firstContact) {
        openThread(firstContact);
        return;
      }
    }
    renderContacts();
  });
}
elements.cancelToken.addEventListener("click", () => elements.tokenDialog.close());
document.querySelector("#token-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const token = elements.tokenInput.value.trim();
  if (!token) return;
  state.token = token;
  localStorage.setItem(TOKEN_KEY, token);
  elements.tokenError.hidden = true;
  elements.tokenDialog.close();
  loadContacts();
  connectSocket();
});

window.addEventListener("beforeunload", () => {
  disconnectSocket();
  clearMedia();
  clearStagedAttachment();
  for (const item of state.optimistic) {
    if (item.localPreviewUrl) URL.revokeObjectURL(item.localPreviewUrl);
  }
});

if (state.token) {
  loadContacts();
  connectSocket();
} else {
  requestToken();
}
