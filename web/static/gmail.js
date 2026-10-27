"use strict";

(() => {
  const TOKEN_STORAGE_KEY = "signal-tui-web-token";
  const PAGE_SIZE = 50;
  const FETCH_LIMIT = 200;
  const MAX_COMPOSE_FILES = 10;
  const MAX_IMAGES_PER_MESSAGE = 50;
  // Reader header/attachments stay compact: names shown before "e altri N",
  // and the attachment list starts collapsed above this many files.
  const RECIPIENTS_PREVIEW = 3;
  const ATTACHMENTS_OPEN_MAX = 4;
  const FOLDER_FALLBACK = "INBOX";
  const RENDER_KEY = "gmail-render-mode";

  const elements = {
    tab: document.getElementById("gmail-tab"),
    tabs: document.getElementById("protocol-tabs"),
    view: document.getElementById("gmail-view"),
    back: document.getElementById("gmail-back"),
    accountStatus: document.getElementById("gmail-account-status"),
    refresh: document.getElementById("gmail-refresh"),
    composeNew: document.getElementById("gmail-compose-new"),
    error: document.getElementById("gmail-error"),
    errorText: document.getElementById("gmail-error-text"),
    errorDismiss: document.getElementById("gmail-error-dismiss"),
    notice: document.getElementById("gmail-notice"),
    noticeText: document.getElementById("gmail-notice-text"),
    listSummary: document.getElementById("gmail-list-summary"),
    listSpinner: document.getElementById("gmail-list-spinner"),
    selectToggle: document.getElementById("gmail-select-toggle"),
    deleteSelected: document.getElementById("gmail-delete-selected"),
    notConfigured: document.getElementById("gmail-not-configured"),
    messages: document.getElementById("gmail-messages"),
    pager: document.getElementById("gmail-pager"),
    pageNewer: document.getElementById("gmail-page-newer"),
    pageOlder: document.getElementById("gmail-page-older"),
    pageInfo: document.getElementById("gmail-page-info"),
    detailEmpty: document.getElementById("gmail-detail-empty"),
    reader: document.getElementById("gmail-reader"),
    readerBack: document.getElementById("gmail-reader-back"),
    readerSubject: document.getElementById("gmail-reader-subject"),
    readerFrom: document.getElementById("gmail-reader-from"),
    readerTo: document.getElementById("gmail-reader-to"),
    readerCcRow: document.getElementById("gmail-reader-cc-row"),
    readerCc: document.getElementById("gmail-reader-cc"),
    readerDate: document.getElementById("gmail-reader-date"),
    readerBody: document.getElementById("gmail-reader-body"),
    renderToggle: document.getElementById("gmail-render-toggle"),
    reply: document.getElementById("gmail-reply"),
    replyAll: document.getElementById("gmail-reply-all"),
    forward: document.getElementById("gmail-forward"),
    archive: document.getElementById("gmail-archive"),
    delete: document.getElementById("gmail-delete"),
    more: document.getElementById("gmail-more"),
    moreMenu: document.getElementById("gmail-more-menu"),
    markUnread: document.getElementById("gmail-mark-unread"),
    attachments: document.getElementById("gmail-attachments"),
    attachmentList: document.getElementById("gmail-attachment-list"),
    attachmentsToggle: document.getElementById("gmail-attachments-toggle"),
    composeDialog: document.getElementById("gmail-compose-dialog"),
    composeForm: document.getElementById("gmail-compose-form"),
    composeTitle: document.getElementById("gmail-compose-title"),
    composeClose: document.getElementById("gmail-compose-close"),
    composeReplyRef: document.getElementById("gmail-compose-reply-ref"),
    composeTo: document.getElementById("gmail-compose-to"),
    composeCc: document.getElementById("gmail-compose-cc"),
    composeBcc: document.getElementById("gmail-compose-bcc"),
    composeSubject: document.getElementById("gmail-compose-subject"),
    composeText: document.getElementById("gmail-compose-text"),
    composeAttach: document.getElementById("gmail-compose-attach"),
    composeFiles: document.getElementById("gmail-compose-files"),
    composeFileSummary: document.getElementById("gmail-compose-file-summary"),
    composeFileList: document.getElementById("gmail-compose-file-list"),
    composeError: document.getElementById("gmail-compose-error"),
    composeCancel: document.getElementById("gmail-compose-cancel"),
    composeSend: document.getElementById("gmail-compose-send"),
  };

  if (!elements.tab || !elements.view || !elements.tabs) return;

  const state = {
    open: false,
    previousProtocol: null,
    status: null,
    configured: false,
    folder: FOLDER_FALLBACK,
    messages: [],
    uidvalidity: null,
    pageIndex: 0,
    listLoading: false,
    listSeq: 0,
    selectedUid: null,
    selectedUids: new Set(),
    selectionMode: false,
    detail: null,
    readerSeq: 0,
    renderMode: localStorage.getItem(RENDER_KEY) === "text" ? "text" : "html",
    composeFiles: [],
    replyToUid: null,
    replyToFolder: null,
    forwardUid: null,
    forwardFolder: null,
    // Compose autocomplete: [{email, name, score, last_ts}] from the server.
    addressBook: [],
    addressBookLoadedAt: 0,
    addressBookLoading: false,
    sending: false,
    deleting: false,
    bulkDeleting: false,
    archiving: false,
  };

  let errorTimer = null;
  let noticeTimer = null;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function parseTimestamp(value) {
    if (value === null || value === undefined || value === "") return null;
    const numeric = Number(value);
    const date = Number.isFinite(numeric)
      ? new Date(numeric < 100000000000 ? numeric * 1000 : numeric)
      : new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatListDate(value) {
    const date = parseTimestamp(value);
    if (!date) return "";
    const now = new Date();
    if (date.toDateString() === now.toDateString()) {
      return new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(date);
    }
    if (date.getFullYear() === now.getFullYear()) {
      return new Intl.DateTimeFormat(undefined, { day: "2-digit", month: "short" }).format(date);
    }
    return new Intl.DateTimeFormat(undefined, { day: "2-digit", month: "2-digit", year: "numeric" }).format(date);
  }

  function formatFullDate(value) {
    const date = parseTimestamp(value);
    if (!date) return "";
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) return "";
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB"];
    let size = bytes / 1024;
    let index = 0;
    while (size >= 1024 && index < units.length - 1) {
      size /= 1024;
      index += 1;
    }
    return `${size.toLocaleString(undefined, { maximumFractionDigits: 1 })} ${units[index]}`;
  }

  function displayAddress(value) {
    if (value === null || value === undefined) return "";
    if (Array.isArray(value)) {
      return value.map(displayAddress).filter(Boolean).join(", ");
    }
    if (typeof value === "object") {
      const candidate = value.name || value.address || value.email;
      return candidate ? String(candidate) : "";
    }
    return String(value);
  }

  function joinAddresses(value) {
    return displayAddress(value).trim();
  }

  // Split an address header on the commas that separate mailboxes, keeping
  // commas inside quoted display names ("Rossi, Mario" <m@example.com>).
  function splitAddresses(value) {
    const text = joinAddresses(value);
    const parts = [];
    let current = "";
    let quoted = false;
    let angle = false;
    for (const char of text) {
      if (char === '"' && !angle) quoted = !quoted;
      else if (char === "<" && !quoted) angle = true;
      else if (char === ">" && !quoted) angle = false;
      if (char === "," && !quoted && !angle) {
        if (current.trim()) parts.push(current.trim());
        current = "";
      } else {
        current += char;
      }
    }
    if (current.trim()) parts.push(current.trim());
    return parts;
  }

  // "Mario Rossi <mario@example.com>" → "Mario Rossi"; bare address otherwise.
  function addressName(address) {
    const named = /^(.*?)\s*<([^<>]*)>\s*$/.exec(address);
    if (!named) return address;
    const name = named[1].trim().replace(/^"(.*)"$/, "$1").trim();
    return name || named[2].trim();
  }

  // Long To/Cc lists stay on one line: the first names plus "e altri N".
  function renderRecipients(element, value) {
    const addresses = splitAddresses(value);
    element.replaceChildren();
    if (!addresses.length) {
      element.textContent = "—";
      return;
    }
    const list = node("span", "gmail-recipients");
    addresses.forEach((address, index) => {
      const item = node("span", "gmail-recipient", addressName(address));
      item.title = address;
      item.hidden = index >= RECIPIENTS_PREVIEW;
      list.append(item);
    });
    element.append(list);
    const extra = addresses.length - RECIPIENTS_PREVIEW;
    if (extra <= 0) return;
    const more = node("button", "gmail-link-button", `e altri ${extra}`);
    more.type = "button";
    more.setAttribute("aria-expanded", "false");
    more.addEventListener("click", () => {
      const expanded = more.getAttribute("aria-expanded") !== "true";
      [...list.children].forEach((item, index) => {
        item.hidden = !expanded && index >= RECIPIENTS_PREVIEW;
      });
      more.setAttribute("aria-expanded", expanded ? "true" : "false");
      more.textContent = expanded ? "mostra meno" : `e altri ${extra}`;
    });
    element.append(more);
  }

  function extractEmail(value) {
    const text = displayAddress(value).trim();
    const bracketed = /<([^<>\s]+@[^<>\s]+)>/.exec(text);
    if (bracketed) return bracketed[1].trim();
    const bare = /[^\s,;<>]+@[^\s,;<>]+/.exec(text);
    return bare ? bare[0].trim() : text;
  }

  function subjectOf(message) {
    const subject = message && typeof message.subject === "string" ? message.subject.trim() : "";
    return subject || "(Nessun oggetto)";
  }

  function senderOf(message) {
    const sender = displayAddress(message && message.from).trim();
    return sender || "(Mittente sconosciuto)";
  }

  function senderNameOf(message) {
    const sender = displayAddress(message && message.from).trim();
    if (!sender) return "";
    // "Mario Rossi <mario@example.com>" → "Mario Rossi"
    const named = /^(.*?)\s*<[^<>]*>\s*$/.exec(sender);
    if (named) {
      const name = named[1].trim().replace(/^"(.*)"$/, "$1").trim();
      if (name) return name;
    }
    // Solo "<mario@example.com>" → indirizzo senza parentesi.
    const bare = /^<([^<>]+)>$/.exec(sender);
    return bare ? bare[1].trim() : sender;
  }

  function senderLabel(message) {
    return senderNameOf(message) || "(Mittente sconosciuto)";
  }

  function normalizeUid(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : String(value);
  }

  function sameUid(first, second) {
    return first !== null && first !== undefined
      && second !== null && second !== undefined
      && String(first) === String(second);
  }

  function uidKey(value) {
    return String(normalizeUid(value));
  }

  function toggleSelectionMode() {
    if (state.bulkDeleting) return;
    state.selectionMode = !state.selectionMode;
    if (!state.selectionMode) state.selectedUids.clear();
    renderList();
    updateSelectionActions();
  }

  function updateSelectionActions() {
    const count = state.selectedUids.size;
    const busy = state.bulkDeleting || state.listLoading;
    const empty = state.messages.length === 0;

    elements.selectToggle.textContent = state.selectionMode ? "Annulla" : "Seleziona";
    elements.selectToggle.setAttribute("aria-pressed", state.selectionMode ? "true" : "false");
    elements.selectToggle.disabled = state.bulkDeleting || state.listLoading || (!state.selectionMode && empty);

    elements.deleteSelected.hidden = !state.selectionMode;
    elements.deleteSelected.disabled = busy || count === 0;
    elements.deleteSelected.setAttribute(
      "aria-label",
      count === 1 ? "Elimina il messaggio selezionato" : `Elimina ${count} messaggi selezionati`,
    );
    elements.deleteSelected.textContent = state.bulkDeleting ? "Eliminazione…" : "Elimina";
    elements.refresh.disabled = busy;
  }

  function setMessageSelected(key, selected, wrapper, checkbox) {
    if (selected) state.selectedUids.add(key);
    else state.selectedUids.delete(key);
    if (checkbox) checkbox.checked = selected;
    if (wrapper) wrapper.classList.toggle("selected", selected);
    updateSelectionActions();
  }

  function syncRowSelection() {
    for (const item of elements.messages.querySelectorAll(".gmail-item")) {
      const checkbox = item.querySelector(".gmail-select");
      if (!checkbox) continue;
      const selected = state.selectedUids.has(checkbox.dataset.uid || "");
      checkbox.checked = selected;
      checkbox.disabled = state.bulkDeleting;
      item.classList.toggle("selected", selected);
    }
  }

  function safeFilename(name, fallback) {
    const cleaned = String(name || "")
      .replace(/[\\/\u0000-\u001f\u007f]/g, "_")
      .replace(/^\.+/, "")
      .trim()
      .slice(0, 160);
    return cleaned || fallback;
  }

  function filenameFromDisposition(value) {
    if (!value) return "";
    const encoded = /filename\*\s*=\s*([^;]+)/i.exec(value);
    if (encoded) {
      let raw = encoded[1].trim().replace(/^"|"$/g, "");
      const parts = raw.split("''");
      if (parts.length > 1) raw = parts.slice(1).join("''");
      try {
        return decodeURIComponent(raw);
      } catch {
        return raw;
      }
    }
    const plain = /filename\s*=\s*"([^"]*)"|filename\s*=\s*([^;]+)/i.exec(value);
    if (!plain) return "";
    return (plain[1] ?? plain[2] ?? "").trim().replace(/^"|"$/g, "");
  }

  function makeEmpty(title, text) {
    const container = node("div", "gmail-empty");
    container.append(node("strong", null, title));
    if (text) container.append(node("p", null, text));
    return container;
  }

  function storedToken() {
    try {
      return localStorage.getItem(TOKEN_STORAGE_KEY) || "";
    } catch {
      return "";
    }
  }

  async function directFetch(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.body instanceof FormData) {
      headers.delete("Content-Type");
    } else if (typeof options.body === "string" && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    const token = storedToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    return fetch(path, { ...options, headers });
  }

  function detailFromPayload(payload) {
    const raw = payload && typeof payload === "object" ? payload.detail : "";
    if (typeof raw === "string") return raw.trim();
    if (Array.isArray(raw)) {
      return raw
        .map((item) => {
          if (typeof item === "string") return item;
          if (item && typeof item.msg === "string") {
            const location = Array.isArray(item.loc)
              ? item.loc.filter((part) => typeof part === "string" || typeof part === "number").join(".")
              : "";
            return location ? `${location}: ${item.msg}` : item.msg;
          }
          return "";
        })
        .filter(Boolean)
        .join("; ");
    }
    return "";
  }

  async function buildError(response) {
    const status = response.status;
    let detail = "";
    try {
      const payload = await response.clone().json();
      detail = detailFromPayload(payload);
    } catch {
      detail = "";
    }
    const fallback = {
      400: "Richiesta non valida.",
      401: "Token web non valido o sessione scaduta.",
      404: "Messaggio o allegato non trovato.",
      413: "Allegati troppo grandi (max 10 file e 25 MB totali).",
      502: "Gmail non ha risposto. Riprova tra poco.",
      503: "Gmail non è configurato.",
    }[status] || `Errore ${status}.`;
    const error = new Error(detail || fallback);
    error.status = status;
    return error;
  }

  async function request(path, options = {}) {
    let response;
    try {
      response = typeof window.apiFetch === "function"
        ? await window.apiFetch(path, options)
        : await directFetch(path, options);
    } catch (error) {
      if (error && error.response) throw await buildError(error.response);
      if (error && error.name === "AbortError") throw error;
      throw new Error("Errore di rete: impossibile contattare il server.");
    }
    if (!response.ok) throw await buildError(response);
    return response;
  }

  function showError(message) {
    window.clearTimeout(errorTimer);
    elements.errorText.textContent = String(message || "Errore imprevisto.");
    elements.error.hidden = false;
    errorTimer = window.setTimeout(() => {
      elements.error.hidden = true;
      errorTimer = null;
    }, 12000);
  }

  function hideError() {
    window.clearTimeout(errorTimer);
    errorTimer = null;
    elements.error.hidden = true;
  }

  function showNotice(message) {
    window.clearTimeout(noticeTimer);
    elements.noticeText.textContent = String(message || "");
    elements.notice.hidden = false;
    noticeTimer = window.setTimeout(() => {
      elements.notice.hidden = true;
      noticeTimer = null;
    }, 5000);
  }

  function hideNotice() {
    window.clearTimeout(noticeTimer);
    noticeTimer = null;
    elements.notice.hidden = true;
  }

  function setAccountStatus(text) {
    elements.accountStatus.textContent = text;
    elements.accountStatus.title = text;
  }

  function renderAccountStatus() {
    const status = state.status;
    if (!status) {
      setAccountStatus("Stato non disponibile");
      return;
    }
    if (!status.configured) {
      setAccountStatus("Non configurato");
      return;
    }
    const parts = [];
    if (status.address) parts.push(String(status.address));
    if (state.folder) parts.push(state.folder);
    if (status.connected === false) parts.push("connessione non disponibile");
    setAccountStatus(parts.join(" · "));
  }

  function setListLoading(loading) {
    state.listLoading = loading;
    elements.listSpinner.hidden = !loading;
    elements.messages.setAttribute("aria-busy", loading ? "true" : "false");
    elements.refresh.disabled = loading;
    renderPager();
    updateSelectionActions();
  }

  function pageCount() {
    return Math.max(1, Math.ceil(state.messages.length / PAGE_SIZE));
  }

  function clampPageIndex() {
    if (state.pageIndex < 0) state.pageIndex = 0;
    const lastPage = pageCount() - 1;
    if (state.pageIndex > lastPage) state.pageIndex = lastPage;
  }

  function pageRange() {
    const start = state.pageIndex * PAGE_SIZE;
    const end = Math.min(start + PAGE_SIZE, state.messages.length);
    return { start, end };
  }

  function visibleMessages() {
    const { start, end } = pageRange();
    return state.messages.slice(start, end);
  }

  function renderListSummary() {
    if (!state.configured) return;
    const total = state.messages.length;
    if (!total) {
      elements.listSummary.textContent = "Nessun messaggio.";
      return;
    }
    const { start, end } = pageRange();
    const parts = [];
    if (total <= PAGE_SIZE) parts.push(`${total} messaggi`);
    else parts.push(`${start + 1}–${end} di ${total}`);
    const unread = state.messages.filter((message) => !message.seen).length;
    if (unread) parts.push(`${unread} non letti`);
    if (state.folder) parts.push(state.folder);
    elements.listSummary.textContent = parts.join(" · ");
  }

  function renderPager() {
    const total = state.messages.length;
    // Only when there is more than one page to move between.
    const visible = state.configured && total > 0 && pageCount() > 1;
    elements.pager.hidden = !visible;
    if (!visible) return;
    clampPageIndex();
    const { start, end } = pageRange();
    elements.pageInfo.textContent = `${start + 1}–${end} di ${total}`;
    elements.pageNewer.disabled = state.listLoading || state.bulkDeleting || state.pageIndex <= 0;
    elements.pageOlder.disabled = state.listLoading || state.bulkDeleting || state.pageIndex >= pageCount() - 1;
  }

  function changePage(delta) {
    const target = state.pageIndex + delta;
    if (target < 0 || target >= pageCount()) return;
    state.pageIndex = target;
    elements.messages.scrollTop = 0;
    renderList();
  }

  function messageAriaLabel(message, selected) {
    const parts = [];
    if (state.selectionMode) {
      parts.push(selected
        ? `Deseleziona messaggio di ${senderLabel(message)}.`
        : `Seleziona messaggio di ${senderLabel(message)}.`);
    }
    if (!message.seen) parts.push("Non letto.");
    parts.push(`Da ${senderLabel(message)}.`);
    parts.push(`${subjectOf(message)}.`);
    if (message.has_attachments) parts.push("Contiene allegati.");
    return parts.join(" ");
  }

  function messageItem(message) {
    const wrapper = node("div", "gmail-item");
    wrapper.setAttribute("role", "listitem");

    const key = uidKey(message.uid);
    let checkbox = null;
    if (state.selectionMode) {
      checkbox = node("input", "gmail-select");
      checkbox.type = "checkbox";
      checkbox.dataset.uid = key;
      checkbox.checked = state.selectedUids.has(key);
      checkbox.disabled = state.bulkDeleting;
      checkbox.setAttribute(
        "aria-label",
        `Seleziona messaggio di ${senderLabel(message)}: ${subjectOf(message)}`,
      );
      checkbox.addEventListener("change", () => {
        setMessageSelected(key, checkbox.checked, wrapper, checkbox);
      });
      if (checkbox.checked) wrapper.classList.add("selected");
    }

    const button = node("button", "gmail-message");
    button.type = "button";
    if (!message.seen) button.classList.add("unread");
    if (sameUid(state.selectedUid, message.uid)) {
      button.classList.add("active");
      button.setAttribute("aria-current", "true");
    }
    button.setAttribute(
      "aria-label",
      messageAriaLabel(message, state.selectionMode && state.selectedUids.has(key)),
    );

    const top = node("span", "gmail-row-top");
    top.append(node("span", "gmail-sender", senderLabel(message)));
    top.append(node("span", "gmail-date", formatListDate(message.date_ts)));
    button.append(top);
    button.append(node("span", "gmail-subject", subjectOf(message)));
    if (message.snippet) button.append(node("span", "gmail-snippet", message.snippet));

    const badges = node("span", "gmail-badges");
    if (!message.seen) badges.append(node("span", "gmail-badge unread", "Non letto"));
    if (message.has_attachments) badges.append(node("span", "gmail-badge", "Allegati"));
    if (badges.childElementCount) button.append(badges);

    button.addEventListener("click", () => {
      if (state.selectionMode) {
        if (state.bulkDeleting) return;
        const selected = !state.selectedUids.has(key);
        setMessageSelected(key, selected, wrapper, checkbox);
        button.setAttribute("aria-label", messageAriaLabel(message, selected));
        return;
      }
      void openMessage(message.uid);
    });
    if (checkbox) wrapper.append(checkbox);
    wrapper.append(button);
    return wrapper;
  }

  function renderList() {
    clampPageIndex();
    const fragment = document.createDocumentFragment();
    const messages = visibleMessages();
    if (!messages.length) {
      const empty = makeEmpty("Inbox vuota", "Non ci sono messaggi da mostrare.");
      empty.setAttribute("role", "listitem");
      fragment.append(empty);
    } else {
      for (const message of messages) fragment.append(messageItem(message));
    }
    elements.messages.replaceChildren(fragment);
    syncRowSelection();
    renderListSummary();
    renderPager();
    updateSelectionActions();
  }

  function renderListPlaceholder(title, text) {
    const empty = makeEmpty(title, text);
    empty.setAttribute("role", "listitem");
    elements.messages.replaceChildren(empty);
    elements.pager.hidden = true;
    updateSelectionActions();
  }

  function setDetailOpen(open) {
    elements.view.classList.toggle("gmail-detail-open", open);
  }

  function closeReader() {
    state.readerSeq += 1;
    state.selectedUid = null;
    state.detail = null;
    elements.reader.hidden = true;
    elements.reader.setAttribute("aria-busy", "false");
    elements.detailEmpty.hidden = false;
    elements.attachments.hidden = true;
    elements.attachmentList.replaceChildren();
    cancelPendingImages({ all: true });
    elements.readerBody.replaceChildren();
    elements.readerBody.classList.remove("is-empty", "is-html");
    elements.renderToggle.hidden = true;
    setDetailOpen(false);
    renderList();
  }

  function showReaderLoading() {
    elements.detailEmpty.hidden = true;
    elements.reader.hidden = false;
    // The whole reader scrolls (header, attachments and body together).
    elements.reader.scrollTop = 0;
    elements.reader.setAttribute("aria-busy", "true");
    elements.readerSubject.textContent = "Caricamento messaggio…";
    elements.readerFrom.textContent = "";
    elements.readerTo.textContent = "";
    elements.readerCcRow.hidden = true;
    elements.readerCc.textContent = "";
    elements.readerDate.textContent = "";
    cancelPendingImages({ all: true });
    elements.readerBody.textContent = "Caricamento…";
    elements.readerBody.classList.remove("is-html");
    elements.readerBody.classList.add("is-empty");
    elements.renderToggle.hidden = true;
    elements.attachments.hidden = true;
    elements.attachmentList.replaceChildren();
  }

  function showReaderError(message) {
    elements.reader.setAttribute("aria-busy", "false");
    elements.readerSubject.textContent = "Impossibile aprire il messaggio";
    elements.readerFrom.textContent = "";
    elements.readerTo.textContent = "";
    elements.readerCcRow.hidden = true;
    elements.readerCc.textContent = "";
    elements.readerDate.textContent = "";
    cancelPendingImages({ all: true });
    elements.readerBody.textContent = String(message || "Errore sconosciuto.");
    elements.readerBody.classList.remove("is-html");
    elements.readerBody.classList.add("is-empty");
    elements.renderToggle.hidden = true;
    elements.attachments.hidden = true;
    elements.attachmentList.replaceChildren();
  }

  function renderAttachments(detail) {
    const attachments = Array.isArray(detail.attachments) ? detail.attachments : [];
    elements.attachmentList.replaceChildren();
    elements.attachments.hidden = attachments.length === 0;
    if (!attachments.length) return;
    const uid = detail.uid !== undefined && detail.uid !== null ? detail.uid : state.selectedUid;

    // Images shown by the body via cid: are listed apart, collapsed: they are
    // part of the message, not files the sender attached.
    const files = [];
    const inlineImages = [];
    for (const attachment of attachments) {
      const index = Number(attachment && attachment.index);
      if (!Number.isInteger(index) || index < 0) continue;
      const filename = safeFilename(attachment && attachment.filename, `allegato-${index + 1}`);
      const chip = node("button", "gmail-attachment-chip");
      chip.type = "button";
      const metaParts = [];
      if (attachment && attachment.content_type) metaParts.push(String(attachment.content_type));
      let size = "";
      if (attachment && attachment.size !== undefined && attachment.size !== null) {
        size = formatBytes(attachment.size);
        if (size) metaParts.push(size);
      }
      chip.title = `Scarica ${filename}${metaParts.length ? ` (${metaParts.join(" · ")})` : ""}`;
      chip.setAttribute("aria-label", `Scarica ${filename}`);
      chip.append(node("span", "gmail-attachment-name", filename));
      if (size) chip.append(node("span", "gmail-attachment-meta", size));
      chip.addEventListener("click", () => {
        void downloadAttachment(uid, index, filename, chip);
      });
      (attachment && attachment.inline === true ? inlineImages : files).push(chip);
    }
    if (!files.length && !inlineImages.length) {
      elements.attachments.hidden = true;
      return;
    }

    elements.attachmentList.append(...files);
    if (inlineImages.length) {
      for (const chip of inlineImages) {
        chip.classList.add("is-inline");
        chip.hidden = files.length > 0;
      }
      elements.attachmentList.append(...inlineImages);
      if (files.length) {
        const more = node(
          "button",
          "gmail-link-button",
          `+ ${inlineImages.length} ${inlineImages.length === 1 ? "immagine" : "immagini"} nel testo`,
        );
        more.type = "button";
        more.addEventListener("click", () => {
          for (const chip of inlineImages) chip.hidden = false;
          more.remove();
        });
        elements.attachmentList.append(more);
      }
    }
    const label = files.length
      ? `Allegati (${files.length})`
      : `Immagini nel testo (${inlineImages.length})`;
    setAttachmentsExpanded(files.length > 0 && files.length <= ATTACHMENTS_OPEN_MAX, label);
  }

  // Quoted text (marked data-quote by the server) is folded behind a "•••"
  // button, as in Gmail: "block" folds the element, "after" the element and
  // everything after it (Outlook).  Nested quotes fold with their outer one,
  // and nothing folds when the quote would be the whole message (forwards).
  function collapseQuotes(root) {
    const groups = [];
    for (const marker of root.querySelectorAll("[data-quote]")) {
      if (marker.parentElement && marker.parentElement.closest("[data-quote]")) continue;
      if (groups.some((group) => group.includes(marker))) continue;
      const group = [marker];
      if (marker.getAttribute("data-quote") === "after") {
        for (let next = marker.nextElementSibling; next; next = next.nextElementSibling) {
          group.push(next);
        }
      }
      groups.push(group);
    }
    if (!groups.length) return;
    const quoted = groups.flat().reduce((total, element) => total + element.textContent.trim().length, 0);
    if (root.textContent.trim().length - quoted <= 0) return;

    for (const group of groups) {
      const toggle = node("button", "gmail-quote-toggle", "•••");
      toggle.type = "button";
      toggle.title = "Mostra testo citato";
      toggle.setAttribute("aria-label", "Mostra testo citato");
      toggle.setAttribute("aria-expanded", "false");
      for (const element of group) element.hidden = true;
      toggle.addEventListener("click", () => {
        const expanded = toggle.getAttribute("aria-expanded") !== "true";
        for (const element of group) element.hidden = !expanded;
        toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
        const label = expanded ? "Nascondi testo citato" : "Mostra testo citato";
        toggle.title = label;
        toggle.setAttribute("aria-label", label);
      });
      group[0].before(toggle);
    }
  }

  function setAttachmentsExpanded(expanded, label) {
    const toggle = elements.attachmentsToggle;
    if (label !== undefined) toggle.dataset.label = label;
    toggle.textContent = toggle.dataset.label || "Allegati";
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    elements.attachmentList.hidden = !expanded;
  }

  function toggleAttachments() {
    setAttachmentsExpanded(elements.attachmentsToggle.getAttribute("aria-expanded") !== "true");
  }

  function safeImageUrl(value) {
    const candidate = String(value || "").trim();
    // Relative ("gmail-image?...") so it resolves under a reverse-proxy prefix;
    // a legacy root-absolute form from older cached HTML is made relative.
    if (/^\/?gmail-image\?token=[A-Za-z0-9._-]+(?:&v=\d+)?$/.test(candidate)) return candidate.replace(/^\//, "");
    if (/^data:image\/(?:png|jpe?g|gif|webp);base64,/i.test(candidate)) return candidate;
    return "";
  }

  function cancelPendingImages({ all = false } = {}) {
    for (const image of elements.readerBody.querySelectorAll("img[src]")) {
      if (!all && image.dataset.loaded === "1") continue;
      image.removeAttribute("src");
    }
  }

  // Mails designed for a fixed width (td width="700", 600px tables…) wider
  // than the reader are scaled down to fit, as mobile mail apps do, instead
  // of scrolling sideways.  Below HTML_FIT_MIN_ZOOM the text would be
  // unreadable: the remaining overflow scrolls.
  const HTML_FIT_MIN_ZOOM = 0.5;
  let htmlFitFrame = 0;

  function fitHtmlToWidth() {
    const fit = elements.readerBody.querySelector(".gmail-html-fit");
    if (!fit) return;
    fit.style.zoom = "";
    const available = fit.clientWidth;
    const needed = fit.scrollWidth;
    if (!available || needed <= available + 1) return;
    fit.style.zoom = String(Math.max(HTML_FIT_MIN_ZOOM, available / needed));
  }

  function scheduleHtmlFit() {
    if (htmlFitFrame) return;
    htmlFitFrame = window.requestAnimationFrame(() => {
      htmlFitFrame = 0;
      fitHtmlToWidth();
    });
  }

  function loadImages() {
    const seq = state.readerSeq;
    // Cell/table backgrounds (often a newsletter's hero banners): the server
    // turned background="…" / background-image:url(…) into an inert data-bg
    // with a signed proxy URL, applied here like an <img> src.
    const backgrounds = [...elements.readerBody.querySelectorAll("[data-bg]")];
    const images = [...elements.readerBody.querySelectorAll("img[data-src]")];
    const total = backgrounds.length + images.length;
    let loaded = 0;
    for (const element of backgrounds) {
      if (loaded >= MAX_IMAGES_PER_MESSAGE) break;
      if (element.style.backgroundImage) continue;
      const url = safeImageUrl(element.dataset.bg || "");
      if (!url) continue;
      element.style.backgroundImage = `url("${url}")`;
      loaded += 1;
    }
    for (const image of images) {
      if (loaded >= MAX_IMAGES_PER_MESSAGE) break;
      if (image.hasAttribute("src")) continue;
      const url = safeImageUrl(image.dataset.src || "");
      if (!url) {
        image.classList.add("is-failed");
        continue;
      }
      image.loading = "lazy";
      image.decoding = "async";
      image.addEventListener(
        "error",
        () => {
          if (seq !== state.readerSeq) return;
          image.classList.add("is-failed");
        },
        { once: true },
      );
      image.addEventListener(
        "load",
        () => {
          if (!image.hasAttribute("src")) return;
          image.dataset.loaded = "1";
          image.classList.remove("is-failed");
          scheduleHtmlFit(); // a loaded image may widen the mail
        },
        { once: true },
      );
      image.src = url;
      loaded += 1;
    }
    if (total > MAX_IMAGES_PER_MESSAGE) {
      showNotice(`Caricate ${loaded} di ${total} immagini.`);
    }
  }

  function renderReader(detail) {
    cancelPendingImages({ all: true });
    elements.reader.setAttribute("aria-busy", "false");
    elements.readerSubject.textContent = subjectOf(detail);
    elements.readerFrom.textContent = senderOf(detail) || "—";
    renderRecipients(elements.readerTo, detail.to);
    const cc = joinAddresses(detail.cc);
    elements.readerCcRow.hidden = !cc;
    renderRecipients(elements.readerCc, detail.cc);
    elements.readerDate.textContent = formatFullDate(detail.date_ts) || "—";

    const text = typeof detail.text === "string" ? detail.text : "";
    const html = typeof detail.html === "string" ? detail.html : "";
    const hasHtml = html.trim() !== "";
    const mode = hasHtml && state.renderMode === "html" ? "html" : "text";
    if (mode === "html") {
      // html is sanitized server-side by nh3 (allow-list, scripts removed,
      // inline styles re-serialized from a strict CSS allow-list, remote
      // images deferred to signed data-src proxy URLs, only http/https/mailto
      // links). Parse it inertly and move the resulting nodes instead of
      // assigning raw markup.
      const parsed = new DOMParser().parseFromString(html, "text/html");
      const content = document.createElement("div");
      content.className = "gmail-html-content";
      // Inner box the shrink-to-fit zoom applies to (see fitHtmlToWidth).
      const fit = document.createElement("div");
      fit.className = "gmail-html-fit";
      fit.append(...parsed.body.childNodes);
      collapseQuotes(fit);
      content.append(fit);
      elements.readerBody.replaceChildren(content);
      scheduleHtmlFit();
      elements.readerBody.classList.remove("is-empty");
      elements.readerBody.classList.add("is-html");
    } else {
      elements.readerBody.textContent = text || "Nessun contenuto testuale.";
      elements.readerBody.classList.remove("is-html");
      elements.readerBody.classList.toggle("is-empty", !text);
    }
    // A "More actions" menu item: the label says what clicking it does.
    elements.renderToggle.hidden = !hasHtml;
    elements.renderToggle.textContent = mode === "html" ? "Mostra come testo" : "Mostra HTML";

    if (mode === "html") loadImages();
    renderAttachments(detail);
  }

  function toggleRenderMode() {
    if (!state.detail) return;
    const html = typeof state.detail.html === "string" ? state.detail.html : "";
    if (!html.trim()) return;
    state.renderMode = state.renderMode === "html" ? "text" : "html";
    localStorage.setItem(RENDER_KEY, state.renderMode);
    renderReader(state.detail);
  }

  async function markRead(uid, folder) {
    try {
      await request(
        `api/gmail/messages/${encodeURIComponent(String(uid))}/read?folder=${encodeURIComponent(folder)}`,
        { method: "POST" },
      );
      let changed = false;
      for (const message of state.messages) {
        if (sameUid(message.uid, uid) && !message.seen) {
          message.seen = true;
          changed = true;
        }
      }
      if (changed) {
        renderList();
        renderListSummary();
      }
    } catch (error) {
      if (error && error.name !== "AbortError" && error.status !== 401) {
        console.debug("[gmail] mark read failed", error.status || "network");
      }
    }
  }

  async function openMessage(uid) {
    if (elements.composeDialog.open) return;
    state.selectedUid = normalizeUid(uid);
    state.detail = null;
    const sequence = ++state.readerSeq;
    renderList();
    setDetailOpen(true);
    showReaderLoading();

    try {
      const response = await request(
        `api/gmail/messages/${encodeURIComponent(String(state.selectedUid))}?folder=${encodeURIComponent(state.folder)}`,
      );
      const detail = await response.json();
      if (sequence !== state.readerSeq) return;
      if (!detail || typeof detail !== "object") throw new Error("Risposta del messaggio non valida.");
      state.detail = detail;
      renderReader(detail);
      void markRead(state.selectedUid, state.folder);
    } catch (error) {
      if (sequence !== state.readerSeq) return;
      if (error && error.name === "AbortError") return;
      showReaderError(error.message);
      showError(error.message);
    }
  }

  function setMoreMenu(open) {
    elements.moreMenu.hidden = !open;
    elements.more.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function messagePath(uid, action) {
    return `api/gmail/messages/${encodeURIComponent(String(uid))}/${action}?folder=${encodeURIComponent(state.folder)}`;
  }

  async function archiveMessage() {
    const uid = state.selectedUid;
    if (uid === null || uid === undefined || state.archiving) return;
    state.archiving = true;
    elements.archive.disabled = true;
    try {
      await request(messagePath(uid, "archive"), { method: "POST" });
      closeReader();
      showNotice("Messaggio archiviato.");
      await loadMessages({ force: true });
    } catch (error) {
      if (error && error.name === "AbortError") return;
      showError(error.message);
    } finally {
      state.archiving = false;
      elements.archive.disabled = false;
    }
  }

  async function markUnread() {
    setMoreMenu(false);
    const uid = state.selectedUid;
    if (uid === null || uid === undefined) return;
    try {
      await request(messagePath(uid, "unread"), { method: "POST" });
      const message = state.messages.find((item) => item.uid === uid);
      if (message) message.seen = false;
      // Back to the list, as in Gmail: reopening it would mark it read again.
      closeReader();
      showNotice("Segnato come non letto.");
    } catch (error) {
      if (error && error.name === "AbortError") return;
      showError(error.message);
    }
  }

  async function deleteMessage() {
    if (state.deleting) return;
    const uid = state.selectedUid;
    if (uid === null || uid === undefined) return;
    if (!window.confirm("Spostare il messaggio nel cestino?")) return;

    state.deleting = true;
    // An icon-only button: never rewrite its content (that would drop the
    // SVG); disabled + aria-busy show the pending state.
    const button = elements.delete;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      await request(
        `api/gmail/messages/${encodeURIComponent(String(uid))}?folder=${encodeURIComponent(state.folder)}`,
        { method: "DELETE" },
      );
      closeReader();
      await loadMessages({ force: true });
    } catch (error) {
      if (error && error.name === "AbortError") return;
      const message = error.status === 404
        ? "Messaggio non trovato: potrebbe essere già stato eliminato."
        : error.status === 502
          ? "Gmail non ha risposto: riprova tra poco."
          : error.status === 503
            ? "Gmail non è configurato."
            : error.message;
      showError(message);
    } finally {
      state.deleting = false;
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  async function deleteSelected() {
    if (state.bulkDeleting) return;
    const uids = Array.from(state.selectedUids).map((key) => normalizeUid(key));
    if (!uids.length) return;
    const count = uids.length;
    if (!window.confirm(`Spostare ${count} messaggi nel cestino?`)) return;

    state.bulkDeleting = true;
    syncRowSelection();
    updateSelectionActions();
    renderPager();

    try {
      await request("api/gmail/messages/bulk-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ uids, folder: state.folder }),
      });

      state.selectedUids.clear();
      await loadMessages({ force: true });
    } catch (error) {
      if (error && error.name === "AbortError") return;
      const status = error && error.status;
      const fallback = error && error.message ? error.message : "Errore imprevisto.";
      const message = status === 400
        ? fallback
        : status === 502
          ? "Gmail non ha risposto: riprova tra poco."
          : status === 503
            ? "Gmail non è configurato."
            : fallback;
      showError(message);
    } finally {
      state.bulkDeleting = false;
      syncRowSelection();
      updateSelectionActions();
      renderPager();
    }
  }

  async function downloadAttachment(uid, index, fallbackName, button) {
    // Chips keep their size label: only the name shows the progress.
    const label = button.querySelector(".gmail-attachment-name") || button;
    const originalText = label.textContent;
    button.disabled = true;
    label.textContent = "Download…";
    try {
      const response = await request(
        `api/gmail/messages/${encodeURIComponent(String(uid))}/attachments/${encodeURIComponent(String(index))}?folder=${encodeURIComponent(state.folder)}`,
      );
      const blob = await response.blob();
      const filename = safeFilename(
        filenameFromDisposition(response.headers.get("Content-Disposition")) || fallbackName,
        `allegato-${index + 1}`,
      );
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      anchor.rel = "noopener";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1500);
    } catch (error) {
      if (error && error.name !== "AbortError") showError(error.message);
    } finally {
      button.disabled = false;
      label.textContent = originalText;
    }
  }

  async function loadMessages({ force = false } = {}) {
    if (!state.configured || !state.open) return;

    state.selectedUids.clear();
    state.selectionMode = false;
    syncRowSelection();
    updateSelectionActions();

    const sequence = ++state.listSeq;
    setListLoading(true);
    elements.listSummary.textContent = "Caricamento…";

    const params = new URLSearchParams({
      folder: state.folder,
      offset: "0",
      limit: String(FETCH_LIMIT),
    });
    if (force) params.set("refresh", "1");

    try {
      const response = await request(`api/gmail/messages?${params.toString()}`);
      const payload = await response.json();
      if (sequence !== state.listSeq) return;
      if (!payload || !Array.isArray(payload.messages)) {
        throw new Error("Risposta della inbox non valida.");
      }

      const parsedUidvalidity = Number(payload.uidvalidity);
      const nextUidvalidity = Number.isFinite(parsedUidvalidity) && parsedUidvalidity > 0
        ? parsedUidvalidity
        : null;
      const mailboxChanged = state.uidvalidity !== null && nextUidvalidity !== null
        && nextUidvalidity !== state.uidvalidity;
      if (nextUidvalidity !== null) state.uidvalidity = nextUidvalidity;

      const messages = payload.messages
        .filter((message) => message && message.uid !== null && message.uid !== undefined && message.uid !== "")
        .map((message) => ({
          uid: normalizeUid(message.uid),
          from: displayAddress(message.from),
          to: displayAddress(message.to),
          subject: typeof message.subject === "string" ? message.subject : "",
          date_ts: message.date_ts ?? null,
          snippet: typeof message.snippet === "string" ? message.snippet : "",
          has_attachments: Boolean(message.has_attachments),
          seen: Boolean(message.seen),
        }));

      if (typeof payload.folder === "string" && payload.folder) state.folder = payload.folder;

      state.messages = messages;
      state.pageIndex = 0;
      renderList();
      renderAccountStatus();
      if (mailboxChanged) {
        showNotice("La casella è cambiata: lista aggiornata.");
        if (state.selectedUid !== null) closeReader();
      }
      if (state.selectedUid !== null
        && !state.messages.some((message) => sameUid(message.uid, state.selectedUid))) {
        closeReader();
      }
    } catch (error) {
      if (sequence !== state.listSeq) return;
      if (error && error.name === "AbortError") return;
      showError(error.message);
      state.messages = [];
      state.pageIndex = 0;
      renderListPlaceholder("Impossibile caricare la inbox", error.message);
      elements.listSummary.textContent = "Errore di caricamento.";
    } finally {
      if (sequence === state.listSeq) setListLoading(false);
    }
  }

  async function loadStatus({ force = false } = {}) {
    if (!state.open) return;
    hideError();
    state.listSeq += 1;
    state.listLoading = false;
    elements.refresh.disabled = true;
    elements.listSpinner.hidden = false;
    elements.messages.setAttribute("aria-busy", "false");
    elements.listSummary.textContent = "Caricamento stato…";
    if (!state.status) setAccountStatus("Caricamento…");

    try {
      const response = await request("api/gmail/status");
      const payload = await response.json();
      if (!payload || typeof payload !== "object") throw new Error("Risposta di stato non valida.");
      state.status = payload;
      state.configured = Boolean(payload.configured);
      state.folder = typeof payload.folder === "string" && payload.folder ? payload.folder : FOLDER_FALLBACK;
      // Archiving removes the Inbox label: it only applies to the inbox.
      elements.archive.hidden = state.folder.toUpperCase() !== "INBOX";
      renderAccountStatus();
      elements.composeNew.disabled = !state.configured;

      if (!state.configured) {
        state.listSeq += 1;
        state.listLoading = false;
        state.messages = [];
        state.uidvalidity = null;
        state.pageIndex = 0;
        state.selectedUids.clear();
        state.selectionMode = false;
        updateSelectionActions();
        elements.listSpinner.hidden = true;
        elements.refresh.disabled = false;
        elements.messages.setAttribute("aria-busy", "false");
        elements.notConfigured.hidden = false;
        elements.messages.hidden = true;
        elements.messages.replaceChildren();
        elements.pager.hidden = true;
        elements.listSummary.textContent = "Gmail non configurato.";
        closeReader();
        return;
      }

      elements.notConfigured.hidden = true;
      elements.messages.hidden = false;
      await loadMessages({ force });
    } catch (error) {
      if (error && error.name === "AbortError") return;
      state.configured = false;
      state.status = null;
      state.messages = [];
      state.pageIndex = 0;
      state.selectedUids.clear();
      state.selectionMode = false;
      updateSelectionActions();
      elements.listSpinner.hidden = true;
      elements.refresh.disabled = false;
      elements.composeNew.disabled = true;
      elements.notConfigured.hidden = true;
      elements.messages.hidden = false;
      elements.messages.setAttribute("aria-busy", "false");
      elements.listSummary.textContent = "Errore di stato.";
      renderListPlaceholder("Impossibile leggere lo stato di Gmail", error.message);
      renderAccountStatus();
      setAccountStatus("Errore di stato");
      showError(error.message);
    }
  }

  function renderComposeFiles() {
    elements.composeFileList.replaceChildren();
    if (!state.composeFiles.length) {
      elements.composeFileSummary.textContent = "Nessun allegato";
      return;
    }
    const total = state.composeFiles.reduce((sum, file) => sum + (Number(file.size) || 0), 0);
    const label = state.composeFiles.length === 1 ? "1 allegato" : `${state.composeFiles.length} allegati`;
    elements.composeFileSummary.textContent = `${label} · ${formatBytes(total)}`;

    state.composeFiles.forEach((file, index) => {
      const row = node("div", "gmail-file");
      const name = node("span", "gmail-file-name", file.name || `allegato-${index + 1}`);
      name.title = file.name || "";
      const size = node("span", "gmail-file-size", formatBytes(file.size));
      const remove = node("button", "gmail-file-remove", "Rimuovi");
      remove.type = "button";
      remove.setAttribute("aria-label", `Rimuovi ${file.name || `allegato-${index + 1}`}`);
      remove.addEventListener("click", () => {
        state.composeFiles.splice(index, 1);
        renderComposeFiles();
      });
      row.append(name, size, remove);
      elements.composeFileList.append(row);
    });
  }

  function resetCompose() {
    state.composeFiles = [];
    state.replyToUid = null;
    state.replyToFolder = null;
    elements.composeFiles.value = "";
    elements.composeFileList.replaceChildren();
    elements.composeFileSummary.textContent = "Nessun allegato";
    elements.composeError.hidden = true;
    elements.composeError.textContent = "";
    elements.composeReplyRef.hidden = true;
    elements.composeReplyRef.textContent = "";
  }

  function showComposeError(message) {
    elements.composeError.textContent = String(message || "Invio non riuscito.");
    elements.composeError.hidden = false;
  }

  const COMPOSE_TITLES = {
    new: "Nuova email",
    reply: "Rispondi",
    replyAll: "Rispondi a tutti",
    forward: "Inoltra",
  };

  function prefixedSubject(subject, prefix, already) {
    const original = typeof subject === "string" ? subject.trim() : "";
    if (already.test(original)) return original;
    return original ? `${prefix} ${original}` : prefix;
  }

  // Reply all: the sender and the original To in "A", the original Cc in
  // "Cc"; each address once, never the account's own.
  function replyAllRecipients(detail) {
    const own = String((state.status && state.status.address) || "").trim().toLowerCase();
    const seen = new Set(own ? [own] : []);
    const pick = (value) => splitAddresses(value)
      .map((address) => extractEmail(address))
      .filter((email) => {
        const key = email.toLowerCase();
        if (!email || seen.has(key)) return false;
        seen.add(key);
        return true;
      });
    const to = [...pick(detail.from), ...pick(detail.to)];
    const cc = pick(detail.cc);
    // Replying to all on a mail we sent to ourselves only: back to the sender.
    if (!to.length && !cc.length) to.push(extractEmail(detail.from));
    return { to, cc };
  }

  function forwardedFileCount(detail) {
    const attachments = Array.isArray(detail.attachments) ? detail.attachments : [];
    return attachments.filter((attachment) => attachment && attachment.inline !== true).length;
  }

  // ── Address autocomplete (A / Cc / Ccn) ──────────────────────────────────
  // The server's address book comes from the latest Sent and Inbox headers;
  // it is loaded once per compose (cached 15 min on the server) and filtered
  // here as the user types.
  const ADDRESS_SUGGESTIONS_MAX = 8;

  function normalizeSearch(text) {
    return String(text || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  }

  // The comma-separated address the caret is in: [start, end) of the field.
  function addressTokenRange(value, caret) {
    const before = value.lastIndexOf(",", Math.max(0, caret - 1));
    const after = value.indexOf(",", caret);
    return { start: before + 1, end: after === -1 ? value.length : after };
  }

  function suggestAddresses(query, book, exclude) {
    const q = normalizeSearch(query).trim();
    if (!q || !Array.isArray(book)) return [];
    const ranked = [];
    for (const contact of book) {
      if (!contact || !contact.email || exclude.has(contact.email)) continue;
      const email = contact.email.toLowerCase();
      const name = normalizeSearch(contact.name);
      let rank = -1;
      if (email.startsWith(q) || name.startsWith(q)) rank = 0;
      else if (name.split(/\s+/).some((word) => word.startsWith(q))
        || email.split(/[@._+-]/).some((part) => part.startsWith(q))) rank = 1;
      else if (name.includes(q) || email.includes(q)) rank = 2;
      if (rank >= 0) ranked.push({ contact, rank });
    }
    ranked.sort((a, b) => a.rank - b.rank
      || (b.contact.score || 0) - (a.contact.score || 0)
      || (b.contact.last_ts || 0) - (a.contact.last_ts || 0));
    return ranked.slice(0, ADDRESS_SUGGESTIONS_MAX).map((item) => item.contact);
  }

  function formatAddress(contact) {
    const name = String(contact.name || "").replace(/["<>\r\n]/g, "").trim();
    if (!name) return contact.email;
    const quoted = /[,;:@()[\]\\.]/.test(name) ? `"${name}"` : name;
    return `${quoted} <${contact.email}>`;
  }

  // Replace the address under the caret with *contact*, then ", ".
  function applySuggestion(value, caret, contact) {
    const { start, end } = addressTokenRange(value, caret);
    const head = value.slice(0, start).replace(/\s+$/, "");
    const tail = value.slice(end).replace(/^,\s*/, "");
    const inserted = `${head ? `${head} ` : ""}${formatAddress(contact)}, `;
    return { value: inserted + tail, caret: inserted.length };
  }

  // Drop the ", " the autocomplete leaves after the last address (and any
  // empty item): the server's address parser rejects a list ending in ",".
  function tidyAddressField(value) {
    return String(value || "")
      .replace(/,(\s*,)+/g, ",")
      .replace(/^[\s,]+|[\s,]+$/g, "");
  }

  function emailsInField(value) {
    return new Set(splitAddresses(value).map((address) => extractEmail(address).toLowerCase()));
  }

  async function loadAddressBook() {
    const fresh = state.addressBookLoadedAt && Date.now() - state.addressBookLoadedAt < 5 * 60 * 1000;
    if (fresh || state.addressBookLoading) return;
    state.addressBookLoading = true;
    try {
      const response = await request("api/gmail/address-book");
      const payload = await response.json();
      state.addressBook = Array.isArray(payload && payload.contacts) ? payload.contacts : [];
      state.addressBookLoadedAt = Date.now();
    } catch {
      // No suggestions: typing addresses still works.
    } finally {
      state.addressBookLoading = false;
    }
  }

  function attachAddressAutocomplete(input) {
    const list = node("ul", "gmail-suggestions");
    list.id = `${input.id}-suggestions`;
    list.setAttribute("role", "listbox");
    list.hidden = true;
    input.after(list);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("aria-controls", list.id);
    let items = [];
    let active = -1;

    const close = () => {
      items = [];
      active = -1;
      list.hidden = true;
      list.replaceChildren();
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
    };
    const highlight = (index) => {
      active = index;
      [...list.children].forEach((option, position) => {
        option.setAttribute("aria-selected", position === index ? "true" : "false");
      });
      if (index >= 0) input.setAttribute("aria-activedescendant", list.children[index].id);
      else input.removeAttribute("aria-activedescendant");
    };
    const accept = (index) => {
      const contact = items[index];
      if (!contact) return;
      const next = applySuggestion(input.value, input.selectionStart ?? input.value.length, contact);
      input.value = next.value;
      input.setSelectionRange(next.caret, next.caret);
      close();
      input.focus();
    };
    const update = () => {
      const caret = input.selectionStart ?? input.value.length;
      const { start } = addressTokenRange(input.value, caret);
      const query = input.value.slice(start, caret);
      items = suggestAddresses(query, state.addressBook, emailsInField(input.value));
      if (!items.length) {
        close();
        return;
      }
      list.replaceChildren(...items.map((contact, index) => {
        const option = node("li", "gmail-suggestion");
        option.id = `${list.id}-${index}`;
        option.setAttribute("role", "option");
        if (contact.name) option.append(node("span", "gmail-suggestion-name", contact.name));
        option.append(node("span", "gmail-suggestion-email", contact.email));
        // mousedown, not click: keep the focus (and the caret) in the input.
        option.addEventListener("mousedown", (event) => {
          event.preventDefault();
          accept(index);
        });
        return option;
      }));
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      highlight(0);
    };

    input.addEventListener("input", update);
    // Leaving the field drops the trailing ", " kept for the next address.
    // Picking a suggestion does not blur it (options act on mousedown).
    input.addEventListener("blur", () => {
      close();
      const tidy = tidyAddressField(input.value);
      if (tidy !== input.value) input.value = tidy;
    });
    input.addEventListener("keydown", (event) => {
      if (list.hidden) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        highlight((active + step + items.length) % items.length);
      } else if ((event.key === "Enter" || event.key === "Tab") && active >= 0) {
        event.preventDefault();
        accept(active);
      } else if (event.key === "Escape") {
        // Close the list, not the compose dialog.
        event.preventDefault();
        event.stopPropagation();
        close();
      }
    });
    return close;
  }

  const closeAddressSuggestions = [
    elements.composeTo,
    elements.composeCc,
    elements.composeBcc,
  ].map(attachAddressAutocomplete);

  function openCompose(mode, detail) {
    for (const close of closeAddressSuggestions) close();
    void loadAddressBook();
    state.composeFiles = [];
    state.replyToUid = null;
    state.replyToFolder = null;
    state.forwardUid = null;
    state.forwardFolder = null;
    elements.composeForm.reset();
    resetCompose();
    elements.composeTitle.textContent = COMPOSE_TITLES[mode] || COMPOSE_TITLES.new;

    const uid = detail && detail.uid !== undefined && detail.uid !== null ? detail.uid : state.selectedUid;
    if ((mode === "reply" || mode === "replyAll") && detail) {
      if (mode === "replyAll") {
        const { to, cc } = replyAllRecipients(detail);
        elements.composeTo.value = to.join(", ");
        elements.composeCc.value = cc.join(", ");
      } else {
        elements.composeTo.value = extractEmail(detail.from);
      }
      elements.composeSubject.value = prefixedSubject(detail.subject, "Re:", /^re:/i);
      state.replyToUid = uid;
      state.replyToFolder = state.folder;
      const label = mode === "replyAll" ? "Risposta a tutti" : "Risposta a";
      elements.composeReplyRef.textContent = mode === "replyAll"
        ? `${label} — ${subjectOf(detail)}`
        : `${label} ${senderOf(detail)} — ${subjectOf(detail)}`;
      elements.composeReplyRef.hidden = false;
    } else if (mode === "forward" && detail) {
      elements.composeSubject.value = prefixedSubject(detail.subject, "Fwd:", /^(fwd?|i):/i);
      state.forwardUid = uid;
      state.forwardFolder = state.folder;
      const files = forwardedFileCount(detail);
      const suffix = files ? ` (con ${files} ${files === 1 ? "allegato" : "allegati"})` : "";
      elements.composeReplyRef.textContent = `Inoltro di ${senderOf(detail)} — ${subjectOf(detail)}${suffix}`;
      elements.composeReplyRef.hidden = false;
    }

    renderComposeFiles();
    if (!elements.composeDialog.open) {
      if (typeof elements.composeDialog.showModal === "function") {
        elements.composeDialog.showModal();
      } else {
        elements.composeDialog.setAttribute("open", "");
      }
    }
    window.requestAnimationFrame(() => {
      if (mode === "reply" || mode === "replyAll") elements.composeText.focus();
      else elements.composeTo.focus();
    });
  }

  function closeCompose() {
    if (elements.composeDialog.open) {
      elements.composeDialog.close();
    } else {
      resetCompose();
    }
  }

  function setSending(sending) {
    state.sending = sending;
    elements.composeForm.setAttribute("aria-busy", sending ? "true" : "false");
    elements.composeSend.disabled = sending;
    elements.composeSend.textContent = sending ? "Invio…" : "Invia";
    elements.composeClose.disabled = sending;
    elements.composeCancel.disabled = sending;
    elements.composeAttach.disabled = sending;
  }

  async function sendCompose(event) {
    event.preventDefault();
    if (state.sending) return;
    elements.composeError.hidden = true;
    elements.composeError.textContent = "";

    const form = new FormData();
    form.append("to", tidyAddressField(elements.composeTo.value));
    form.append("cc", tidyAddressField(elements.composeCc.value));
    form.append("bcc", tidyAddressField(elements.composeBcc.value));
    form.append("subject", elements.composeSubject.value.trim());
    form.append("text", elements.composeText.value);
    if (state.replyToUid !== null && state.replyToUid !== undefined) {
      form.append("reply_to_uid", String(state.replyToUid));
      form.append("reply_to_folder", state.replyToFolder || state.folder);
    }
    if (state.forwardUid !== null && state.forwardUid !== undefined) {
      // The server adds the original text and attachments.
      form.append("forward_uid", String(state.forwardUid));
      form.append("forward_folder", state.forwardFolder || state.folder);
    }
    for (const file of state.composeFiles) {
      form.append("file[]", file, file.name);
    }

    setSending(true);
    try {
      await request("api/gmail/send", { method: "POST", body: form });
      closeCompose();
      showNotice("Email inviata.");
      void loadMessages();
    } catch (error) {
      if (error && error.name === "AbortError") return;
      showComposeError(error.message);
    } finally {
      setSending(false);
    }
  }

  function activateGmail() {
    if (state.open) {
      void loadStatus();
      return;
    }
    const previous = elements.tabs.querySelector("[data-protocol].active");
    state.previousProtocol = previous ? previous.dataset.protocol : null;
    state.open = true;
    document.body.classList.add("gmail-active");
    elements.view.hidden = false;
    elements.tab.classList.add("active");
    elements.tab.setAttribute("aria-selected", "true");
    clearProtocolActive();
    hideError();
    hideNotice();
    window.requestAnimationFrame(() => elements.back.focus());
    window.dispatchEvent(new CustomEvent("gmail:view"));
    void loadStatus();
  }

  function clearProtocolActive() {
    for (const tab of elements.tabs.querySelectorAll("[data-protocol]")) {
      if (tab.classList.contains("active")) tab.classList.remove("active");
      if (tab.getAttribute("aria-selected") !== "false") tab.setAttribute("aria-selected", "false");
    }
  }

  function restoreProtocolTab() {
    if (!state.previousProtocol) return;
    for (const tab of elements.tabs.querySelectorAll("[data-protocol]")) {
      if (tab.dataset.protocol === state.previousProtocol) {
        tab.classList.add("active");
        tab.setAttribute("aria-selected", "true");
        tab.focus();
        return;
      }
    }
  }

  function deactivateGmail({ restoreTab = false } = {}) {
    if (!state.open) return;
    state.open = false;
    state.listSeq += 1;
    state.readerSeq += 1;
    cancelPendingImages();
    state.selectedUids.clear();
    state.selectionMode = false;
    if (elements.composeDialog.open) elements.composeDialog.close();
    document.body.classList.remove("gmail-active");
    elements.view.hidden = true;
    elements.tab.classList.remove("active");
    elements.tab.setAttribute("aria-selected", "false");
    setDetailOpen(false);
    hideError();
    hideNotice();
    if (restoreTab) restoreProtocolTab();
    // app.js shows/hides the tab unread dots depending on the open view.
    if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent("gmail:view"));
  }

  elements.tab.addEventListener("click", activateGmail);
  // The server's unread poller saw new mail: refresh the list if it is open.
  window.addEventListener("gmail:new-mail", () => {
    if (state.open && state.configured) void loadMessages({ force: true });
  });

  elements.tabs.addEventListener("click", (event) => {
    if (!state.open) return;
    const target = event.target instanceof Element ? event.target.closest("[data-protocol]") : null;
    if (target) deactivateGmail({ restoreTab: false });
  });

  elements.back.addEventListener("click", () => deactivateGmail({ restoreTab: true }));
  elements.refresh.addEventListener("click", () => {
    state.selectedUids.clear();
    state.selectionMode = false;
    syncRowSelection();
    updateSelectionActions();
    void loadStatus({ force: true });
  });
  elements.composeNew.addEventListener("click", () => openCompose("new", null));
  elements.reply.addEventListener("click", () => {
    if (state.detail) openCompose("reply", state.detail);
  });
  elements.replyAll.addEventListener("click", () => {
    if (state.detail) openCompose("replyAll", state.detail);
  });
  elements.forward.addEventListener("click", () => {
    if (state.detail) openCompose("forward", state.detail);
  });
  elements.archive.addEventListener("click", () => {
    void archiveMessage();
  });
  elements.more.addEventListener("click", () => {
    setMoreMenu(elements.moreMenu.hidden);
  });
  elements.markUnread.addEventListener("click", () => {
    void markUnread();
  });
  // The menu closes on any click outside it and on Escape.
  document.addEventListener("click", (event) => {
    if (elements.moreMenu.hidden) return;
    if (event.target instanceof Element && event.target.closest(".gmail-more")) return;
    setMoreMenu(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !elements.moreMenu.hidden) {
      setMoreMenu(false);
      elements.more.focus();
    }
  });
  window.addEventListener("resize", scheduleHtmlFit);
  elements.renderToggle.addEventListener("click", () => {
    setMoreMenu(false);
    toggleRenderMode();
  });
  elements.attachmentsToggle.addEventListener("click", toggleAttachments);
  elements.readerBody.addEventListener("click", (event) => {
    const anchor = event.target instanceof Element ? event.target.closest("a[href]") : null;
    if (!anchor) return;
    const href = anchor.getAttribute("href") || "";
    if (!/^(https?:|mailto:)/i.test(href)) return;
    event.preventDefault();
    if (href.toLowerCase().startsWith("mailto:")) {
      window.location.href = href;
    } else {
      window.open(href, "_blank", "noopener,noreferrer");
    }
  });
  elements.delete.addEventListener("click", () => {
    void deleteMessage();
  });
  elements.selectToggle.addEventListener("click", () => {
    toggleSelectionMode();
  });
  elements.deleteSelected.addEventListener("click", () => {
    void deleteSelected();
  });
  elements.readerBack.addEventListener("click", closeReader);
  elements.errorDismiss.addEventListener("click", hideError);
  elements.pageOlder.addEventListener("click", () => {
    changePage(1);
  });
  elements.pageNewer.addEventListener("click", () => {
    changePage(-1);
  });

  elements.composeAttach.addEventListener("click", () => {
    elements.composeFiles.value = "";
    elements.composeFiles.click();
  });
  elements.composeFiles.addEventListener("change", () => {
    const files = Array.from(elements.composeFiles.files || []);
    elements.composeFiles.value = "";
    if (!files.length) return;
    const next = [...state.composeFiles];
    let full = false;
    for (const file of files) {
      if (next.length >= MAX_COMPOSE_FILES) {
        full = true;
        break;
      }
      next.push(file);
    }
    state.composeFiles = next;
    if (full) showComposeError(`Massimo ${MAX_COMPOSE_FILES} allegati.`);
    else elements.composeError.hidden = true;
    renderComposeFiles();
  });

  elements.composeClose.addEventListener("click", () => {
    if (!state.sending) closeCompose();
  });
  elements.composeCancel.addEventListener("click", () => {
    if (!state.sending) closeCompose();
  });
  elements.composeForm.addEventListener("submit", (event) => {
    void sendCompose(event);
  });
  elements.composeDialog.addEventListener("cancel", (event) => {
    if (state.sending) event.preventDefault();
  });
  elements.composeDialog.addEventListener("close", resetCompose);

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !state.open) return;
    if (elements.composeDialog.open) return;
    if (!elements.reader.hidden) closeReader();
  });

  const tabObserver = new MutationObserver(() => {
    if (!document.body.classList.contains("gmail-active")) return;
    clearProtocolActive();
  });
  tabObserver.observe(elements.tabs, {
    subtree: true,
    childList: true,
    attributes: true,
    attributeFilter: ["class", "aria-selected"],
  });
})();
