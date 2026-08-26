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

function scrollThreadToBottom() {
  elements.messages.scrollTop = elements.messages.scrollHeight;
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
    const main = document.createElement("span");
    main.className = "contact-main";
    const name = document.createElement("span");
    name.className = "contact-name";
    name.textContent = contact.display_name || contact.id;
    const icon = document.createElement("span");
    icon.className = "protocol-icon";
    icon.title = contact.protocol;
    icon.innerHTML = protocolIcon(contact.protocol);
    main.append(name, icon);
    copy.append(main);
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

function renderMessages(messages, protocol) {
  clearMedia();
  elements.messages.replaceChildren();
  if (!messages.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Nessun messaggio archiviato in questa conversazione.";
    elements.messages.append(empty);
    scrollThreadToBottom();
    requestAnimationFrame(scrollThreadToBottom);
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
  state.active = contact;
  elements.threadName.textContent = contact.display_name || contact.id;
  elements.threadMeta.innerHTML = `${protocolIcon(contact.protocol, 13)}<span class="thread-proto-name">${contact.protocol}</span> · sola lettura`;
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
if (elements.protocolTabs) {
  elements.protocolTabs.addEventListener("click", (event) => {
    const tab = event.target.closest("[data-protocol]");
    if (!tab) return;
    state.protocolFilter = tab.dataset.protocol;
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
});

if (state.token) {
  loadContacts();
  connectSocket();
} else {
  requestToken();
}
