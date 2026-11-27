// The Email workspace: folders, a paged searchable list and a sandboxed reader (services/mailbox.py).
import { api, el } from "./api.js";
import { ICONS } from "./icons.js";
import { workspaceEmpty as emptyState } from "./workspaceParts.js";

const PAGE_SIZE = 50;
const STROKE = 'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';
const GLYPHS = {
  refresh: `<svg ${STROKE}><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/></svg>`,
  clip: `<svg ${STROKE}><path d="M20 11.5l-8.2 8.2a5 5 0 0 1-7.1-7.1l8.5-8.5a3.3 3.3 0 0 1 4.7 4.7l-8.5 8.5a1.7 1.7 0 0 1-2.4-2.4l7.8-7.8"/></svg>`,
  star: `<svg ${STROKE}><path d="M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.8-5.2 2.8 1-5.8-4.3-4.1 5.9-.9z"/></svg>`,
};
// The reader frame's own policy: no scripts (sandbox), nothing remote, only inline styles and data: images.
const FRAME_POLICY = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:";

function glyph(name, label) {
  const node = el("span", { class: `email-glyph email-glyph-${name}` });
  node.innerHTML = GLYPHS[name] || ICONS[name] || "";
  if (label) node.append(el("span", { class: "sr-only", text: label }));
  return node;
}

function backButton(text, label, onclick, kind) {
  const button = el("button", { type: "button", class: `btn quiet email-back ${kind}`, "aria-label": label, onclick });
  button.append(glyph("back"), el("span", { text }));
  return button;
}

function iconButton(name, label, onclick) {
  const button = el("button", { type: "button", class: "btn quiet email-icon-btn", "aria-label": label, title: label, onclick });
  button.append(glyph(name));
  return button;
}

const status = (error) => Number(String(error?.message || "").split(":")[0]) || 0;
const detail = (error) => String(error?.message || "").replace(/^\d+:\s*/, "") || "Something went wrong.";
const person = (who) => (who?.name || who?.address || "").trim();
const people = (list) => (list || []).map((p) => (p.name && p.address ? `${p.name} <${p.address}>` : p.address || p.name)).join(", ");

function shortDate(iso) {
  if (!iso) return "";
  const when = new Date(iso);
  const now = new Date();
  if (when.toDateString() === now.toDateString()) return when.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return when.toLocaleDateString([], when.getFullYear() === now.getFullYear()
    ? { month: "short", day: "numeric" } : { year: "numeric", month: "short", day: "numeric" });
}

function size(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// Plain text with its web addresses as links; built from nodes, never HTML.
function linkified(text) {
  const pre = el("pre", { class: "email-text" });
  for (const piece of String(text || "").split(/(https?:\/\/[^\s<>"')\]]+)/)) {
    if (!/^https?:\/\//.test(piece)) { if (piece) pre.append(document.createTextNode(piece)); continue; }
    // A sentence's closing punctuation is not part of the address.
    const [, url, tail] = piece.match(/^(.*?)([.,;:!?]*)$/);
    pre.append(el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: url }));
    if (tail) pre.append(document.createTextNode(tail));
  }
  return pre;
}

function frameDocument(html) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${FRAME_POLICY}">`
    + `<meta name="referrer" content="no-referrer"><style>html{color-scheme:light}body{margin:0;padding:2px;font:15px/1.5 system-ui,sans-serif;`
    + `color:#202124;background:#fff;overflow-wrap:anywhere}img{max-width:100%;height:auto}table{max-width:100%}pre{white-space:pre-wrap}</style>`
    + `</head><body>${html}</body></html>`;
}

// The HTML body in a frame that cannot run scripts or load anything; links open outside it.
function htmlFrame(html) {
  const frame = el("iframe", { class: "email-frame", title: "Message body", sandbox: "allow-same-origin", referrerpolicy: "no-referrer" });
  frame.srcdoc = frameDocument(html);
  let observer = null;
  frame.addEventListener("load", () => {
    const doc = frame.contentDocument;
    if (!doc) return;
    const fit = () => { frame.style.height = `${Math.max(120, doc.documentElement.scrollHeight)}px`; };
    fit();
    observer?.disconnect();
    observer = new ResizeObserver(fit);
    observer.observe(doc.documentElement);
    doc.addEventListener("click", (event) => {
      const link = event.target.closest?.("a[href]");
      if (!link) return;
      event.preventDefault();
      if (/^https?:/i.test(link.href)) window.open(link.href, "_blank", "noopener,noreferrer");
    });
  });
  frame.dispose = () => observer?.disconnect();
  return frame;
}

export function emailWorkspace(overview) {
  const state = { accounts: [], folders: {}, view: "overview", account: null, folder: "INBOX", folderName: "Inbox",
                  page: 1, query: "", total: 0, messages: [], ref: null, listToken: 0, readToken: 0, frame: null };

  const rail = el("nav", { class: "email-rail", "aria-label": "Mail folders" });
  const title = el("h2", { class: "email-folder-title", text: "Inbox" });
  const subtitle = el("div", { class: "meta email-folder-account" });
  const searchInput = el("input", { type: "search", class: "email-search-input", placeholder: "Search mail", "aria-label": "Search this folder", enterkeyhint: "search" });
  const searchForm = el("form", { class: "email-search", role: "search" }, [searchInput]);
  const listStatus = el("div", { class: "email-list-status", role: "status", "aria-live": "polite" });
  const list = el("ul", { class: "email-list", "aria-label": "Messages" });
  const range = el("span", { class: "meta email-range" });
  const newer = el("button", { type: "button", class: "btn quiet", text: "Newer", onclick: () => loadList(state.page - 1) });
  const older = el("button", { type: "button", class: "btn quiet", text: "Older", onclick: () => loadList(state.page + 1) });
  const listPane = el("section", { class: "email-list-pane", "aria-label": "Message list" }, [
    el("header", { class: "email-pane-head" }, [
      backButton("Folders", "Back to folders", () => setPane("rail"), "email-back-folders"),
      el("div", { class: "email-pane-title" }, [title, subtitle]),
      iconButton("refresh", "Refresh", () => { refreshFolders(state.account); loadList(state.page); }),
    ]),
    searchForm, listStatus, list,
    el("footer", { class: "email-pager" }, [range, el("div", { class: "email-pager-buttons" }, [newer, older])]),
  ]);
  const reader = el("article", { class: "email-reader", "aria-label": "Message", "aria-live": "polite" });
  const browser = el("div", { class: "email-browser", hidden: true }, [listPane, reader]);
  const main = el("div", { class: "email-main" }, [overview, browser]);
  const node = el("div", { class: "email-workspace", "data-pane": "main" }, [rail, main]);

  searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    state.query = searchInput.value.trim();
    loadList(1);
  });
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && searchInput.value) { searchInput.value = ""; state.query = ""; loadList(1); }
  });
  // Up and Down move between messages; Enter or Space opens the focused one (it is a button).
  list.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const rows = [...list.querySelectorAll(".email-row")];
    const at = rows.indexOf(document.activeElement);
    const next = rows[Math.min(rows.length - 1, Math.max(0, at + (event.key === "ArrowDown" ? 1 : -1)))];
    if (next) { event.preventDefault(); next.focus(); }
  });

  function setPane(pane) {
    node.dataset.pane = pane;
  }

  function drawRail() {
    const overviewButton = el("button", { type: "button", class: "email-rail-item email-rail-overview",
      "aria-current": state.view === "overview" ? "page" : "false", onclick: () => showOverview() });
    overviewButton.append(glyph("brain"), el("span", { class: "email-rail-name", text: "Important today" }));
    rail.replaceChildren(overviewButton, ...state.accounts.map(accountGroup));
  }

  function accountGroup(account) {
    const entry = state.folders[account.id] || { status: "loading" };
    const heading = el("h3", { class: "email-rail-account", text: account.email, title: account.email });
    const items = el("ul", { class: "email-rail-folders" });
    if (entry.status === "loading") items.append(el("li", { class: "meta email-rail-note", text: "Loading folders…" }));
    if (entry.status === "error") {
      items.append(folderItem(account, { path: "INBOX", name: "Inbox", depth: 0, selectable: true }),
        el("li", { class: "email-rail-note" }, [el("span", { class: "meta", text: "Could not load folders. " }),
          el("button", { type: "button", class: "btn quiet", text: "Retry", onclick: () => refreshFolders(account.id) })]));
    }
    for (const folder of entry.list || []) items.append(folderItem(account, folder));
    return el("section", { class: "email-rail-group", "aria-label": account.email }, [heading, items]);
  }

  function folderItem(account, folder) {
    const current = state.view === "folder" && state.account === account.id && state.folder === folder.path;
    if (!folder.selectable) {
      return el("li", { class: "email-rail-parent", style: `--depth:${folder.depth || 0}`, text: folder.name });
    }
    const button = el("button", { type: "button", class: "email-rail-item", style: `--depth:${folder.depth || 0}`,
      "aria-current": current ? "page" : "false", title: folder.path,
      onclick: () => openFolder(account.id, folder.path, folder.name) });
    button.append(el("span", { class: "email-rail-name", text: folder.name }));
    if (folder.unread) button.append(el("span", { class: "email-rail-count", text: String(folder.unread), "aria-label": `${folder.unread} unread` }));
    return el("li", {}, [button]);
  }

  async function refreshFolders(accountId) {
    if (!accountId) return;
    state.folders[accountId] = { ...(state.folders[accountId] || {}), status: state.folders[accountId]?.list ? "ready" : "loading" };
    drawRail();
    try {
      const data = await api(`/api/email/accounts/${encodeURIComponent(accountId)}/folders`);
      state.folders[accountId] = { status: "ready", list: data.folders };
    } catch (error) {
      state.folders[accountId] = { status: "error", error: detail(error) };
    }
    drawRail();
  }

  function showOverview() {
    state.view = "overview";
    overview.hidden = false;
    browser.hidden = true;
    setPane("main");
    drawRail();
  }

  function openFolder(accountId, path, name, { keepReader = false } = {}) {
    const account = state.accounts.find((a) => a.id === accountId);
    if (!account) return;
    state.view = "folder";
    state.account = accountId;
    state.folder = path;
    state.folderName = name || path;
    state.query = "";
    searchInput.value = "";
    overview.hidden = true;
    browser.hidden = false;
    title.textContent = state.folderName;
    subtitle.textContent = account.email;
    searchInput.setAttribute("aria-label", `Search ${state.folderName}`);
    setPane("list");
    drawRail();
    if (!keepReader) showReader(null);
    loadList(1);
  }

  async function loadList(page) {
    if (state.view !== "folder") return;
    const token = ++state.listToken;
    state.page = Math.max(1, page);
    list.setAttribute("aria-busy", "true");
    listStatus.textContent = "Loading messages…";
    newer.disabled = older.disabled = true;
    const params = new URLSearchParams({ folder: state.folder, page: String(state.page), page_size: String(PAGE_SIZE) });
    if (state.query) params.set("q", state.query);
    let data;
    try {
      data = await api(`/api/email/accounts/${encodeURIComponent(state.account)}/mailbox?${params}`);
    } catch (error) {
      if (token !== state.listToken) return;
      list.removeAttribute("aria-busy");
      listStatus.textContent = "";
      range.textContent = "";
      list.replaceChildren(el("li", { class: "email-list-error" }, [
        el("p", { text: `Could not load this folder. ${detail(error)}` }),
        el("button", { type: "button", class: "btn", text: "Try again", onclick: () => loadList(state.page) })]));
      return;
    }
    if (token !== state.listToken) return;
    Object.assign(state, { total: data.total, messages: data.messages });
    list.removeAttribute("aria-busy");
    drawList();
  }

  function drawList() {
    const first = state.total ? (state.page - 1) * PAGE_SIZE + 1 : 0;
    const last = Math.min(state.total, state.page * PAGE_SIZE);
    range.textContent = state.total ? `${first.toLocaleString()}–${last.toLocaleString()} of ${state.total.toLocaleString()}` : "";
    newer.disabled = state.page <= 1;
    older.disabled = last >= state.total;
    listStatus.textContent = state.query ? `${state.total.toLocaleString()} found for “${state.query}”` : "";
    if (!state.messages.length) {
      list.replaceChildren(el("li", { class: "email-list-empty" }, [emptyState({
        title: state.query ? "Nothing matches" : "No messages here",
        hint: state.query ? "Try other words, or clear the search." : "This folder is empty.",
        mascot: null })]));
      return;
    }
    list.replaceChildren(...state.messages.map(row));
  }

  function row(message) {
    const button = el("button", { type: "button", class: `email-row${message.seen ? "" : " unread"}`,
      "aria-current": message.ref === state.ref ? "true" : "false", "data-ref": message.ref,
      onclick: () => openMessage(message.ref) });
    const from = person(message.from) || message.from_header || "(unknown sender)";
    button.append(...[
      el("span", { class: "email-row-dot", "aria-hidden": "true" }),
      message.seen ? null : el("span", { class: "sr-only", text: "Unread, " }),
      el("span", { class: "email-row-from", text: from }),
      el("time", { class: "email-row-date", datetime: message.date || "", title: message.date ? new Date(message.date).toLocaleString() : "", text: shortDate(message.date) }),
      el("span", { class: "email-row-subject", text: message.subject || "(no subject)" }),
      el("span", { class: "email-row-marks" }, [
        message.flagged ? glyph("star", "Starred") : null,
        message.has_attachments ? glyph("clip", "Has attachments") : null]),
    ].filter(Boolean));
    return el("li", {}, [button]);
  }

  function markCurrent() {
    for (const node of list.querySelectorAll(".email-row")) node.setAttribute("aria-current", node.dataset.ref === state.ref ? "true" : "false");
  }

  function showReader(content) {
    state.frame?.dispose?.();
    state.frame = null;
    reader.removeAttribute("aria-busy");
    reader.replaceChildren(content || emptyState({ title: "Nothing selected", hint: "Choose a message to read it here.", mascot: null }));
  }

  async function openMessage(ref, loaded = null) {
    const token = ++state.readToken;
    state.ref = ref;
    markCurrent();
    setPane("reader");
    reader.setAttribute("aria-busy", "true");
    reader.replaceChildren(el("p", { class: "meta email-reader-loading", text: "Opening message…" }));
    let message = loaded;
    try {
      message ||= await api(`/api/email/message?ref=${encodeURIComponent(ref)}`);
    } catch (error) {
      if (token !== state.readToken) return;
      const stale = status(error) === 409;
      showReader(el("div", { class: "email-reader-error" }, [
        readerBack(),
        el("p", { text: stale ? "This folder changed since the list was loaded." : `Could not open this message. ${detail(error)}` }),
        el("button", { type: "button", class: "btn", text: stale ? "Reload the list" : "Try again",
          onclick: () => (stale ? loadList(state.page) : openMessage(ref)) })]));
      return;
    }
    if (token !== state.readToken) return;
    showReader(readerView(message));
    reader.querySelector(".email-reader-subject")?.focus({ preventScroll: true });
  }

  function readerBack() {
    return backButton(state.folderName || "Back", `Back to ${state.folderName || "the list"}`,
      () => { setPane("list"); list.querySelector('.email-row[aria-current="true"]')?.focus(); }, "email-back-list");
  }

  function readerView(message) {
    const meta = el("dl", { class: "email-reader-meta" });
    const line = (label, value) => { if (value) meta.append(el("dt", { text: label }), el("dd", { text: value })); };
    line("From", people([message.from]) || message.from_header);
    line("To", people(message.to));
    line("Cc", people(message.cc));
    if (message.reply_to?.length && people(message.reply_to) !== people([message.from])) line("Reply to", people(message.reply_to));
    line("Date", message.date ? new Date(message.date).toLocaleString([], { dateStyle: "full", timeStyle: "short" }) : message.date_header);

    const parts = [readerBack(), el("h2", { class: "email-reader-subject", tabindex: "-1", text: message.subject || "(no subject)" }), meta];
    if (message.attachments?.length) {
      parts.push(el("ul", { class: "email-attachments", "aria-label": "Attachments" }, message.attachments.map((file) =>
        el("li", { class: "email-attachment", title: file.content_type }, [glyph("clip"),
          el("span", { class: "email-attachment-name", text: file.filename || file.content_type }),
          el("span", { class: "meta", text: size(file.size) })]))));
    }
    if (message.remote_images) {
      parts.push(el("p", { class: "meta email-notice", text: `${message.remote_images} remote ${message.remote_images === 1 ? "image was" : "images were"} blocked, so the sender cannot tell you opened this.` }));
    }
    if (message.truncated) parts.push(el("p", { class: "meta email-notice", text: "This message is very large; only its first part is shown." }));

    const body = el("div", { class: "email-body" });
    const show = (mode) => {
      state.frame?.dispose?.();
      state.frame = mode === "html" ? htmlFrame(message.html) : null;
      body.replaceChildren(state.frame || linkified(message.text || ""));
      for (const button of toggle.querySelectorAll("button")) button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
    };
    const toggle = el("div", { class: "email-body-toggle", role: "group", "aria-label": "Message format" });
    if (message.html && message.text) {
      toggle.append(el("button", { type: "button", class: "btn quiet", "data-mode": "html", text: "Formatted", onclick: () => show("html") }),
        el("button", { type: "button", class: "btn quiet", "data-mode": "text", text: "Plain text", onclick: () => show("text") }));
      parts.push(toggle);
    }
    parts.push(body);
    if (message.html) show("html");
    else if (message.text) show("text");
    else body.append(el("p", { class: "meta", text: "This message has no readable text." }));
    return el("div", { class: "email-reader-content" }, parts);
  }

  return {
    node,
    setAccounts(accounts) {
      state.accounts = accounts;
      // Without an account there is nothing to browse: the overview keeps the whole width, as before.
      node.classList.toggle("email-workspace-empty", !accounts.length);
      for (const id of Object.keys(state.folders)) if (!accounts.some((a) => a.id === id)) delete state.folders[id];
      if (state.view === "folder" && !accounts.some((a) => a.id === state.account)) showOverview();
      drawRail();
      for (const account of accounts) if (!state.folders[account.id]) refreshFolders(account.id);
    },
    showOverview,
    openFolder,
    // Opens a message by reference (triage, and later agent replies), with its folder beside it.
    async openRef(ref) {
      let message;
      try {
        message = await api(`/api/email/message?ref=${encodeURIComponent(ref)}`);
      } catch (error) {
        showReader(null);
        throw error;
      }
      const folder = (state.folders[message.account_id]?.list || []).find((f) => f.path === message.folder);
      openFolder(message.account_id, message.folder, folder?.name || (message.folder === "INBOX" ? "Inbox" : message.folder), { keepReader: true });
      openMessage(ref, message);
    },
    dispose() {
      state.listToken++;
      state.readToken++;
      state.frame?.dispose?.();
    },
  };
}

