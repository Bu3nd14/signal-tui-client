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
};

function showError(message) {
  elements.errorText.textContent = message;
  elements.errorBanner.hidden = false;
}

function requestToken(invalid = false) {
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

function contactInitial(contact) {
  return (contact.display_name || contact.id || "?").trim().charAt(0).toUpperCase();
}

function renderContacts() {
  elements.contacts.replaceChildren();
  const sortedContacts = [...state.contacts].sort((a, b) => Number(b.last_message_ts || 0) - Number(a.last_message_ts || 0));
  const contacts = state.protocolFilter === "all"
    ? sortedContacts
    : sortedContacts.filter((contact) => contact.protocol === state.protocolFilter);
  if (state.active && !contacts.some((contact) => contact.id === state.active.id && contact.protocol === state.active.protocol)) {
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
    const name = document.createElement("span");
    name.className = "contact-name";
    name.textContent = contact.display_name || contact.id;
    const detail = document.createElement("span");
    detail.className = "contact-detail protocol-badge";
    detail.textContent = contact.protocol;
    copy.append(name, detail);
    button.append(avatar, copy);
    if (Number(contact.unread) > 0) {
      const unread = document.createElement("span");
      unread.className = "unread-badge";
      unread.textContent = Number(contact.unread) > 99 ? "99+" : String(contact.unread);
      button.append(unread);
    }
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
    image.addEventListener("load", () => container.querySelector(".attachment-loading")?.remove(), { once: true });
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

function renderMessages(messages, protocol) {
  clearMedia();
  elements.messages.replaceChildren();
  if (!messages.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Nessun messaggio archiviato in questa conversazione.";
    elements.messages.append(empty);
    return;
  }
  for (const item of messages) {
    const message = document.createElement("article");
    message.className = `message ${item.direction === "out" ? "out" : "in"}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    const isImage = item.attachment?.type?.toLowerCase().startsWith("image/");
    if (isImage) {
      bubble.append(imageAttachment(item.attachment, protocol));
    }
    const attachmentId = item.attachment?.attachment_id || "";
    const attachmentName = item.attachment?.name || attachmentId.split("?", 1)[0].split("/").filter(Boolean).pop() || "Allegato";
    const displayText = item.text || (item.attachment && !isImage ? attachmentName : "");
    if (displayText) {
      const text = document.createElement("div");
      text.className = "message-text";
      text.textContent = displayText;
      bubble.append(text);
    }
    const time = document.createElement("time");
    time.className = "message-time";
    time.textContent = formatTimestamp(item.timestamp);
    bubble.append(time);
    message.append(bubble);
    elements.messages.append(message);
  }
  elements.messages.scrollTop = elements.messages.scrollHeight;
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
    if (state.active?.id === active.id && state.active?.protocol === active.protocol) renderMessages(messages, active.protocol);
  } catch (error) {
    if (error.name !== "AbortError" && error.message !== "unauthorized") showError("Errore di rete durante il caricamento dei messaggi.");
  } finally {
    if (state.messageRequest === controller) state.messageRequest = null;
  }
}

function openThread(contact) {
  state.active = contact;
  elements.threadName.textContent = contact.display_name || contact.id;
  elements.threadMeta.textContent = `${contact.protocol} · sola lettura`;
  elements.app.classList.add("thread-open");
  renderContacts();
  elements.messages.replaceChildren();
  const loading = document.createElement("div");
  loading.className = "empty-state";
  loading.textContent = "Caricamento messaggi…";
  elements.messages.append(loading);
  loadMessages();
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
elements.protocolTabs.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-protocol]");
  if (!tab) return;
  state.protocolFilter = tab.dataset.protocol;
  renderContacts();
});
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
});

if (state.token) {
  loadContacts();
  connectSocket();
} else {
  requestToken();
}
