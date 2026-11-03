"use strict";

(() => {
  const POLL_MS = 3000;

  const $ = (id) => document.getElementById(id);
  const el = {
    count: $("mail-count"),
    health: $("health"),
    autoRefresh: $("auto-refresh"),
    refresh: $("refresh"),
    deleteAll: $("delete-all"),
    search: $("search"),
    list: $("mail-list"),
    listEmpty: $("list-empty"),
    placeholder: $("detail-placeholder"),
    view: $("mail-view"),
    back: $("back"),
    subject: $("mv-subject"),
    from: $("mv-from"),
    to: $("mv-to"),
    cc: $("mv-cc"),
    date: $("mv-date"),
    id: $("mv-id"),
    size: $("mv-size"),
    flags: $("mv-flags"),
    attachments: $("mv-attachments"),
    download: $("mv-download"),
    del: $("mv-delete"),
    html: $("mv-html"),
    text: $("mv-text"),
    headers: $("mv-headers"),
    raw: $("mv-raw"),
    toast: $("toast"),
  };

  const state = {
    mails: [],
    listKey: "",
    knownIds: null,
    selectedId: null,
    detail: null,
    rawLoadedFor: null,
    tab: "html",
    query: "",
    timer: null,
  };

  // --- HTTP ---------------------------------------------------------------

  async function api(path, options = {}) {
    const res = await fetch(path, { cache: "no-store", ...options });
    if (!res.ok && res.status !== 503) {
      let message = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body && body.error) message = body.error;
      } catch (_) { /* kein JSON */ }
      const err = new Error(message);
      err.status = res.status;
      throw err;
    }
    if (res.status === 204) return null;
    const type = res.headers.get("Content-Type") || "";
    return type.includes("json") ? res.json() : res.text();
  }

  // --- Formatierung -------------------------------------------------------

  const timeFmt = new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit" });
  const dayFmt = new Intl.DateTimeFormat("de-DE", { day: "2-digit", month: "2-digit" });
  const fullFmt = new Intl.DateTimeFormat("de-DE", { dateStyle: "medium", timeStyle: "medium" });

  function shortTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const now = new Date();
    return d.toDateString() === now.toDateString() ? timeFmt.format(d) : dayFmt.format(d);
  }

  function fullDate(mail) {
    const d = new Date(mail.date || mail.received);
    return Number.isNaN(d.getTime()) ? (mail.date || "–") : fullFmt.format(d);
  }

  function formatSize(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function addrLabel(a) {
    return a.name || a.address || "";
  }

  function senderOf(mail) {
    return mail.from.length ? addrLabel(mail.from[0]) : "(unbekannter Absender)";
  }

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderAddresses(target, list) {
    target.replaceChildren();
    if (!list.length) {
      target.append(make("span", "muted", "–"));
      return;
    }
    list.forEach((a, i) => {
      if (i > 0) target.append(", ");
      if (a.name) {
        target.append(make("span", "addr-name", a.name), " ");
        target.append(make("span", "addr-mail", `<${a.address}>`));
      } else {
        target.append(make("span", "addr-name", a.address));
      }
    });
  }

  // --- Toast --------------------------------------------------------------

  let toastTimer = null;
  function toast(message, isError = false) {
    el.toast.textContent = message;
    el.toast.classList.toggle("error", isError);
    el.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.toast.hidden = true; }, 2600);
  }

  // --- Health -------------------------------------------------------------

  async function loadHealth() {
    let checks = {};
    try {
      const data = await api("/health");
      checks = data.checks || {};
    } catch (_) {
      checks = {};
    }
    el.health.querySelectorAll("[data-check]").forEach((dot) => {
      const v = checks[dot.dataset.check];
      dot.classList.toggle("up", v === "up");
      dot.classList.toggle("down", v !== "up");
      dot.title = v === "up" ? "erreichbar" : "nicht erreichbar";
    });
  }

  // --- Liste --------------------------------------------------------------

  function matchesQuery(mail, q) {
    if (!q) return true;
    const hay = [
      mail.subject,
      mail.snippet,
      ...mail.from.map((a) => `${a.name} ${a.address}`),
      ...mail.to.map((a) => `${a.name} ${a.address}`),
      ...mail.cc.map((a) => `${a.name} ${a.address}`),
    ].join(" ").toLowerCase();
    return q.split(/\s+/).every((term) => hay.includes(term));
  }

  function renderList() {
    const q = state.query.trim().toLowerCase();
    const visible = state.mails.filter((m) => matchesQuery(m, q));
    const key = `${q}|${state.selectedId}|` + visible.map((m) => `${m.id}:${m.seen ? 1 : 0}`).join(",");
    if (key === state.listKey) return;
    state.listKey = key;

    const fresh = state.knownIds;
    const items = visible.map((mail) => {
      const li = make("li", "mail-item");
      li.dataset.id = String(mail.id);
      li.tabIndex = 0;
      li.setAttribute("role", "button");
      if (!mail.seen) li.classList.add("unseen");
      if (mail.id === state.selectedId) li.classList.add("active");
      if (fresh && !fresh.has(mail.id)) li.classList.add("fresh");

      li.append(
        make("span", "mi-from", senderOf(mail)),
        make("span", "mi-time", shortTime(mail.received)),
        make("span", "mi-subject", mail.subject || "(kein Betreff)"),
      );
      if (mail.snippet) li.append(make("span", "mi-snippet", mail.snippet));
      return li;
    });
    el.list.replaceChildren(...items);

    el.listEmpty.hidden = visible.length > 0;
    el.listEmpty.querySelector(".empty-title").textContent =
      state.mails.length ? "Keine Treffer" : "Noch keine Mails";
  }

  async function loadMails() {
    try {
      const data = await api("/mails");
      state.mails = data.mails;
      el.count.textContent = String(data.total);
      renderList();
      state.knownIds = new Set(state.mails.map((m) => m.id));

      if (state.selectedId !== null && !state.knownIds.has(state.selectedId)) {
        clearSelection();
      }
    } catch (err) {
      toast(`Mails konnten nicht geladen werden: ${err.message}`, true);
    }
  }

  // --- Detail -------------------------------------------------------------

  function clearSelection() {
    state.selectedId = null;
    state.detail = null;
    el.view.hidden = true;
    el.placeholder.hidden = false;
    document.body.classList.remove("show-detail");
    if (location.hash) history.replaceState(null, "", location.pathname);
    state.listKey = "";
    renderList();
  }

  function setTab(tab) {
    state.tab = tab;
    document.querySelectorAll(".tabs [data-tab]").forEach((b) => {
      b.setAttribute("aria-selected", String(b.dataset.tab === tab));
    });
    document.querySelectorAll(".panes [data-pane]").forEach((p) => {
      p.classList.toggle("active", p.dataset.pane === tab);
    });
    if (tab === "raw") loadRaw();
  }

  async function loadRaw() {
    const id = state.selectedId;
    if (id === null || state.rawLoadedFor === id) return;
    el.raw.textContent = "Lade …";
    try {
      const raw = await api(`/mails/${id}/raw`);
      if (state.selectedId === id) {
        el.raw.textContent = raw;
        state.rawLoadedFor = id;
      }
    } catch (err) {
      el.raw.textContent = `Fehler: ${err.message}`;
    }
  }

  function renderDetail(mail) {
    el.subject.textContent = mail.subject || "(kein Betreff)";
    renderAddresses(el.from, mail.from);
    renderAddresses(el.to, mail.to);
    renderAddresses(el.cc, mail.cc);
    document.querySelectorAll(".cc-row").forEach((n) => { n.hidden = !mail.cc.length; });
    el.date.textContent = fullDate(mail);
    el.date.title = mail.date || "";
    el.id.textContent = `#${mail.id}`;
    el.size.textContent = formatSize(mail.size);

    el.flags.replaceChildren();
    if (mail.flags.length) {
      mail.flags.forEach((f) => el.flags.append(make("span", "flag", f)));
    } else {
      el.flags.append(make("span", "muted", "keine Flags"));
    }

    el.attachments.replaceChildren();
    el.attachments.hidden = !mail.attachments.length;
    mail.attachments.forEach((a) => {
      const chip = make("span", "chip");
      chip.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m21 11-8.5 8.5a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.5 8.5a2 2 0 0 1-3-3L15 7"/></svg>';
      chip.append(`${a.filename || "unbenannt"} · ${a.content_type} · ${formatSize(a.size)}`);
      el.attachments.append(chip);
    });

    el.download.href = `/mails/${mail.id}/raw`;
    el.download.setAttribute("download", `${mail.id}.eml`);

    // Sandbox ohne Freigaben: keine Skripte, keine Formulare, keine Navigation nach außen.
    el.html.srcdoc = mail.html || "";
    el.text.textContent = mail.text != null ? mail.text : "";
    if (mail.text == null) el.text.append(make("span", "muted", "Kein Text-Teil vorhanden."));

    el.headers.replaceChildren(...mail.headers.map(([k, v]) => {
      const tr = make("tr");
      tr.append(make("td", "", k), make("td", "", v));
      return tr;
    }));

    el.raw.textContent = "";
    state.rawLoadedFor = null;

    const htmlTab = document.querySelector('.tabs [data-tab="html"]');
    htmlTab.disabled = !mail.html;
    let tab = state.tab;
    if (tab === "html" && !mail.html) tab = "text";
    if (tab === "text" && mail.text == null && mail.html) tab = "html";
    setTab(tab);

    el.placeholder.hidden = true;
    el.view.hidden = false;
  }

  async function select(id, { pushHash = true } = {}) {
    state.selectedId = id;
    state.listKey = "";
    renderList();
    document.body.classList.add("show-detail");
    if (pushHash && location.hash !== `#${id}`) history.replaceState(null, "", `#${id}`);

    try {
      const mail = await api(`/mails/${id}`);
      if (state.selectedId !== id) return;
      state.detail = mail;
      renderDetail(mail);
    } catch (err) {
      if (err.status === 404) {
        toast(`Mail #${id} existiert nicht mehr.`, true);
        clearSelection();
      } else {
        toast(`Mail konnte nicht geladen werden: ${err.message}`, true);
      }
    }
  }

  // --- Aktionen -----------------------------------------------------------

  async function deleteSelected() {
    const id = state.selectedId;
    if (id === null) return;
    const next = neighbour(1) ?? neighbour(-1);
    try {
      await api(`/mails/${id}`, { method: "DELETE" });
      toast(`Mail #${id} gelöscht`);
    } catch (err) {
      toast(`Löschen fehlgeschlagen: ${err.message}`, true);
      return;
    }
    await loadMails();
    if (next !== null && state.knownIds.has(next)) select(next);
    else clearSelection();
  }

  async function deleteAll() {
    if (!state.mails.length) return;
    if (!confirm(`Alle ${state.mails.length} Mails unwiderruflich löschen?`)) return;
    try {
      const res = await api("/mails", { method: "DELETE" });
      toast(`${res.deleted} Mails gelöscht`);
    } catch (err) {
      toast(`Löschen fehlgeschlagen: ${err.message}`, true);
    }
    clearSelection();
    await loadMails();
  }

  function refresh() {
    return Promise.all([loadMails(), loadHealth()]);
  }

  function neighbour(step) {
    const ids = [...el.list.querySelectorAll(".mail-item")].map((li) => Number(li.dataset.id));
    if (!ids.length) return null;
    const idx = ids.indexOf(state.selectedId);
    if (idx === -1) return step > 0 ? ids[0] : null;
    const n = ids[idx + step];
    return n === undefined ? null : n;
  }

  // --- Polling ------------------------------------------------------------

  function schedule() {
    clearInterval(state.timer);
    state.timer = null;
    if (el.autoRefresh.checked && !document.hidden) {
      state.timer = setInterval(refresh, POLL_MS);
    }
  }

  // --- Events -------------------------------------------------------------

  el.list.addEventListener("click", (e) => {
    const li = e.target.closest(".mail-item");
    if (li) select(Number(li.dataset.id));
  });

  el.list.addEventListener("keydown", (e) => {
    const li = e.target.closest(".mail-item");
    if (li && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      select(Number(li.dataset.id));
    }
  });

  document.querySelector(".tabs").addEventListener("click", (e) => {
    const b = e.target.closest("[data-tab]");
    if (b && !b.disabled) setTab(b.dataset.tab);
  });

  let searchTimer = null;
  el.search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = el.search.value;
      renderList();
    }, 120);
  });

  el.refresh.addEventListener("click", () => refresh().then(() => toast("Aktualisiert")));
  el.deleteAll.addEventListener("click", deleteAll);
  el.del.addEventListener("click", deleteSelected);
  el.back.addEventListener("click", clearSelection);
  el.autoRefresh.addEventListener("change", schedule);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && el.autoRefresh.checked) refresh();
    schedule();
  });

  window.addEventListener("hashchange", () => {
    const id = Number(location.hash.slice(1));
    if (Number.isInteger(id) && id > 0 && id !== state.selectedId) select(id, { pushHash: false });
  });

  document.addEventListener("keydown", (e) => {
    if (e.target instanceof HTMLInputElement || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "/") {
      e.preventDefault();
      el.search.focus();
    } else if (e.key === "j" || e.key === "ArrowDown") {
      const n = neighbour(1);
      if (n !== null) { e.preventDefault(); select(n); }
    } else if (e.key === "k" || e.key === "ArrowUp") {
      const n = neighbour(-1);
      if (n !== null) { e.preventDefault(); select(n); }
    } else if (e.key === "r") {
      refresh();
    } else if (e.key === "Delete") {
      deleteSelected();
    } else if (e.key === "Escape") {
      clearSelection();
    }
  });

  // --- Start --------------------------------------------------------------

  (async () => {
    await refresh();
    const id = Number(location.hash.slice(1));
    if (Number.isInteger(id) && id > 0) select(id, { pushHash: false });
    schedule();
  })();
})();
