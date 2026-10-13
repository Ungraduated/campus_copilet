/* OmniAgent frontend — vanilla JS, Fetch API, Chart.js.
   Security: no secrets in JS or localStorage (only the theme choice); the session lives in an
   HttpOnly cookie and the CSRF token is held in memory. All dynamic text is inserted with
   textContent / escaped before light formatting. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    csrf: null, user: null, domains: [], domain: null, convId: null, convs: [], config: {}, busy: false, charts: [],
    authMode: "login",
  };

  // ───────────── helpers ─────────────
  function el(tag, props = {}, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null) n.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return n;
  }
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function toast(msg) {
    const t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => (t.hidden = true), 2600);
  }

  async function api(path, { method = "GET", body, form } = {}) {
    const headers = {};
    if (method !== "GET" && state.csrf) headers["X-CSRF-Token"] = state.csrf;
    let payload;
    if (form) payload = form;
    else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    let res;
    try { res = await fetch(path, { method, headers, body: payload, credentials: "same-origin" }); }
    catch { throw new Error("Cannot reach the server. Check your connection."); }
    let data = null;
    try { data = await res.json(); } catch { /* empty body */ }
    if (res.status === 401 && !path.startsWith("/api/auth/")) { showAuth(); throw new Error("Please sign in again."); }
    if (!res.ok) throw new Error((data && data.detail) || `Request failed (${res.status}).`);
    return data;
  }

  // Minimal safe markdown: paragraphs, lists, **bold**, `code`
  function renderMarkdown(text) {
    const box = el("div", { class: "prose" });
    const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    let list = null, listType = null, para = [];
    const flushPara = () => { if (para.length) { const p = el("p"); p.innerHTML = inline(para.join(" ")); box.append(p); para = []; } };
    const flushList = () => { list = null; listType = null; };
    for (const raw of String(text).split("\n")) {
      const line = raw.trim();
      const ul = /^[-*•]\s+(.*)/.exec(line), ol = /^\d+[.)]\s+(.*)/.exec(line);
      if (ul || ol) {
        flushPara();
        const type = ul ? "ul" : "ol";
        if (!list || listType !== type) { list = el(type); listType = type; box.append(list); }
        const li = el("li"); li.innerHTML = inline((ul || ol)[1]); list.append(li);
      } else if (!line) { flushPara(); flushList(); }
      else { flushList(); para.push(line); }
    }
    flushPara();
    return box;
  }

  const fmtNum = (v) => (typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : v);
  const isNumType = (t) => t === "integer" || t === "number";

  // ───────────── screens ─────────────
  function showAuth() {
    state.csrf = null; state.user = null;
    $("app").hidden = true; $("auth").hidden = false;
    $("auth-password").value = "";
    destroyCharts();
  }

  function setAuthMode(mode) {
    state.authMode = mode;
    const reg = mode === "register";
    $("auth-title").textContent = reg ? "Create your account" : "Sign in to ask your data";
    $("auth-submit").textContent = reg ? "Create account" : "Sign in";
    $("name-field").hidden = !reg; $("auth-hint").hidden = !reg;
    $("auth-switch-text").textContent = reg ? "Already have an account?" : "New here?";
    $("auth-switch").textContent = reg ? "Sign in" : "Create an account";
    $("auth-password").autocomplete = reg ? "new-password" : "current-password";
    $("auth-error").hidden = true;
  }

  async function submitAuth(e) {
    e.preventDefault();
    const err = $("auth-error"); err.hidden = true;
    const email = $("auth-email").value.trim(), password = $("auth-password").value;
    if (!email || !password) { err.textContent = "Enter your email and password."; err.hidden = false; return; }
    const reg = state.authMode === "register";
    const body = reg ? { email, password, display_name: $("auth-name").value.trim() || email.split("@")[0] } : { email, password };
    $("auth-submit").disabled = true;
    try {
      const data = await api(reg ? "/api/auth/register" : "/api/auth/login", { method: "POST", body });
      $("auth-password").value = "";
      await enterApp(data);
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
    finally { $("auth-submit").disabled = false; }
  }

  async function enterApp(payload) {
    state.csrf = payload.csrf_token; state.user = payload.user; state.domains = payload.domains;
    $("auth").hidden = true; $("app").hidden = false;
    $("user-name").textContent = state.user.display_name;
    $("user-role").textContent = state.user.role;
    $("user-initial").textContent = (state.user.display_name[0] || "?").toUpperCase();
    try { state.config = await api("/api/config"); } catch { state.config = {}; }
    const sel = $("domain-select"); sel.replaceChildren();
    state.domains.forEach((d) => sel.append(el("option", { value: d.key, text: d.name })));
    $("admin-section").hidden = state.user.role !== "admin";
    $("refresh-schema").hidden = state.user.role !== "admin";
    $("docs-section").hidden = !state.config.rag_enabled;
    if (!state.domains.length) { renderNoAccess(); return; }
    await selectDomain(state.domains[0].key);
  }

  function renderNoAccess() {
    $("domain-title").textContent = "No data access yet"; $("domain-desc").textContent = "";
    $("conv-list").replaceChildren();
    $("messages").replaceChildren(el("div", { class: "empty" }, el("h3", { text: "You don't have access to any domain yet" }),
      el("p", { text: "Ask an administrator to grant you access to a domain, then sign in again." })));
    $("send").disabled = true; $("prompt").disabled = true;
  }

  async function selectDomain(key) {
    const d = state.domains.find((x) => x.key === key);
    if (!d) return;
    state.domain = d; state.convId = null;
    $("domain-select").value = key;
    $("domain-title").textContent = d.name; $("domain-desc").textContent = d.description;
    $("send").disabled = false; $("prompt").disabled = false;
    renderEmpty();
    await loadConversations();
  }

  // ───────────── conversations ─────────────
  async function loadConversations() {
    try { state.convs = await api(`/api/conversations?domain=${encodeURIComponent(state.domain.key)}`); }
    catch { state.convs = []; }
    renderConvList();
  }

  function renderConvList() {
    const nav = $("conv-list"); nav.replaceChildren();
    if (!state.convs.length) { nav.append(el("p", { class: "conv-empty", text: "No conversations yet." })); return; }
    for (const c of state.convs) {
      nav.append(el("div", { class: "conv-item" + (c.id === state.convId ? " active" : "") },
        el("button", { class: "open", type: "button", title: c.title, text: c.title, onclick: () => openConversation(c.id) }),
        el("button", { class: "del", type: "button", "aria-label": `Delete conversation ${c.title}`, text: "✕", onclick: () => deleteConversation(c.id) })));
    }
  }

  async function openConversation(id) {
    try {
      const c = await api(`/api/conversations/${id}`);
      state.convId = c.id; closeSidebar();
      const thread = startThread();
      c.messages.forEach((m) => thread.append(renderMessage(m)));
      renderConvList(); scrollDown();
    } catch (ex) { toast(ex.message); }
  }

  async function deleteConversation(id) {
    if (!confirm("Delete this conversation?")) return;
    try {
      await api(`/api/conversations/${id}`, { method: "DELETE" });
      if (state.convId === id) { state.convId = null; renderEmpty(); }
      await loadConversations();
    } catch (ex) { toast(ex.message); }
  }

  async function clearConversation() {
    if (!state.convId) { renderEmpty(); return; }
    if (!confirm("Clear all messages in this conversation?")) return;
    try {
      await api(`/api/conversations/${state.convId}/clear`, { method: "POST" });
      renderEmpty(); await loadConversations();
    } catch (ex) { toast(ex.message); }
  }

  // ───────────── rendering ─────────────
  function destroyCharts() { state.charts.forEach((c) => c.destroy()); state.charts = []; }

  function startThread() {
    destroyCharts();
    const thread = el("div", { class: "thread" });
    $("messages").replaceChildren(thread);
    return thread;
  }

  function renderEmpty() {
    destroyCharts();
    state.convId = null;
    const d = state.domain;
    const examples = (d && d.examples) || [];
    const box = el("div", { class: "empty" },
      el("h3", { text: `Ask about ${d ? d.name.toLowerCase() : "your data"}` }),
      el("p", { text: "Type a question in plain language. I'll query the database, then explain what I found." }),
      el("div", { class: "chips" }, examples.map((q) => el("button", { class: "chip", type: "button", text: q, onclick: () => send(q) }))));
    $("messages").replaceChildren(box);
    renderConvList();
  }

  function scrollDown() { const m = $("messages"); m.scrollTop = m.scrollHeight; }

  function renderMessage(m) {
    if (m.role === "user") return el("div", { class: "msg user" }, el("div", { class: "bubble", text: m.content }));
    const answer = el("div", { class: "answer" }, renderMarkdown(m.content));
    (m.data || []).forEach((b) => { const r = renderBlock(b); if (r) answer.append(r); });
    if (m.steps && m.steps.length) {
      answer.append(el("details", { class: "steps" }, el("summary", { text: `How I got this (${m.steps.length} step${m.steps.length > 1 ? "s" : ""})` }),
        el("pre", { text: m.steps.map((s) => `${s.tool}${s.error ? " ✗ " + s.error : ""}\n${JSON.stringify(s.args || {}, null, 2)}`).join("\n\n") })));
    }
    answer.append(el("div", { class: "msg-tools" },
      el("button", { class: "tiny", type: "button", text: "Copy answer", onclick: () => copy(m.content) })));
    return el("div", { class: "msg assistant" }, answer);
  }

  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast("Copied to clipboard"); }
    catch { toast("Copy is not available in this browser."); }
  }

  function renderBlock(b) {
    if (b.type === "sources") return renderSources(b);
    if (b.type === "aggregate") return renderAggregate(b);
    if (b.type === "table") return renderTable(b);
    return null;
  }

  function buildTable(b) {
    const head = el("tr", {}, b.columns.map((c) => el("th", { class: isNumType(c.type) ? "num" : "", scope: "col", text: c.name.replace(/_/g, " ") })));
    const body = el("tbody", {}, b.rows.map((r) => el("tr", {}, r.map((v, i) => {
      const num = isNumType(b.columns[i].type);
      return v === null ? el("td", { class: "null", text: "—" }) : el("td", { class: num ? "num" : "", text: num ? fmtNum(v) : String(v) });
    }))));
    return el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, head), body));
  }

  function csvEscape(v) {
    let s = v === null || v === undefined ? "" : String(v);
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s; // neutralise spreadsheet formula injection
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }
  function downloadCsv(b) {
    const lines = [b.columns.map((c) => csvEscape(c.name)).join(",")].concat(b.rows.map((r) => r.map(csvEscape).join(",")));
    const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" }));
    const a = el("a", { href: url, download: `${b.title || "results"}.csv` });
    document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function footer(b) {
    const total = b.total_rows;
    const msg = total != null && total > b.rows.length ? `Showing ${b.rows.length} of ${total.toLocaleString()} rows — ask for the next page to see more`
      : `${b.rows.length.toLocaleString()} row${b.rows.length === 1 ? "" : "s"}${b.has_more ? " (more available)" : ""}`;
    return el("div", { class: "result-foot" }, el("span", { text: msg }),
      el("button", { class: "tiny", type: "button", text: "Download CSV", onclick: () => downloadCsv(b) }));
  }

  function renderTable(b) {
    if (!b.rows.length) return el("div", { class: "result" }, el("div", { class: "result-head" }, el("h4", { text: b.title }), el("span", { text: "No matching rows" })));
    return el("div", { class: "result" },
      el("div", { class: "result-head" }, el("h4", { text: b.title }), el("span", { text: `${(b.total_rows ?? b.rows.length).toLocaleString()} matching` })),
      buildTable(b), footer(b));
  }

  const PALETTE = ["#1f7a6d", "#e39b12", "#7a4e8c", "#c4572f", "#3f6ea8", "#6b8f2a", "#a33b6a", "#4d5b6a"];

  function renderAggregate(b) {
    const wrap = el("div", { class: "result" }, el("div", { class: "result-head" }, el("h4", { text: b.title }), el("span", { text: `${b.rows.length} row${b.rows.length === 1 ? "" : "s"}` })));
    if (!b.rows.length) { wrap.firstChild.lastChild.textContent = "No data"; return wrap; }
    const groups = b.group_by || [], metrics = b.metrics || [];
    if (!groups.length && b.rows.length === 1) {  // summary cards
      wrap.append(el("div", { class: "cards" }, metrics.map((m) => {
        const idx = b.columns.findIndex((c) => c.name === m);
        return el("div", { class: "card" }, el("strong", { text: fmtNum(b.rows[0][idx]) ?? "—" }), el("span", { text: m.replace(/_/g, " ") }));
      })));
      return wrap;
    }
    const chart = buildChart(b, groups, metrics);
    if (chart) wrap.append(chart);
    wrap.append(buildTable(b), footer(b));
    return wrap;
  }

  function buildChart(b, groups, metrics) {
    if (!window.Chart || !groups.length || !metrics.length || b.rows.length < 2 || b.rows.length > 60) return null;
    const gi = groups.map((g) => b.columns.findIndex((c) => c.name === g));
    const mi = metrics.map((m) => b.columns.findIndex((c) => c.name === m));
    const timeLike = /_(day|month|year)$/.test(groups[0]);
    let labels, datasets;
    if (groups.length === 1) {
      labels = b.rows.map((r) => String(r[gi[0]]));
      datasets = mi.slice(0, 3).map((idx, k) => ({ label: metrics[k].replace(/_/g, " "), data: b.rows.map((r) => Number(r[idx])), backgroundColor: PALETTE[k], borderColor: PALETTE[k], tension: .25 }));
    } else if (groups.length === 2 && metrics.length === 1) {   // grouped series by 2nd dimension
      labels = [...new Set(b.rows.map((r) => String(r[gi[0]])))];
      const series = [...new Set(b.rows.map((r) => String(r[gi[1]])))].slice(0, 8);
      datasets = series.map((s, k) => ({
        label: s, backgroundColor: PALETTE[k % PALETTE.length], borderColor: PALETTE[k % PALETTE.length],
        data: labels.map((l) => { const row = b.rows.find((r) => String(r[gi[0]]) === l && String(r[gi[1]]) === s); return row ? Number(row[mi[0]]) : 0; }),
      }));
    } else return null;
    const canvas = el("canvas", { role: "img", "aria-label": `Chart of ${metrics.join(", ")} by ${groups.join(", ")}` });
    const box = el("div", { class: "chart-box" }, canvas);
    queueMicrotask(() => {
      const css = getComputedStyle(document.documentElement);
      const grid = css.getPropertyValue("--line").trim(), text = css.getPropertyValue("--muted").trim();
      state.charts.push(new Chart(canvas, {
        type: timeLike ? "line" : "bar", data: { labels, datasets },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: datasets.length > 1, labels: { color: text } } },
          scales: { x: { ticks: { color: text }, grid: { display: false } }, y: { beginAtZero: true, ticks: { color: text }, grid: { color: grid } } },
        },
      }));
    });
    return box;
  }

  function renderSources(b) {
    return el("div", { class: "result" }, el("div", { class: "result-head" }, el("h4", { text: "Sources" }), el("span", { text: `${b.items.length} passage${b.items.length === 1 ? "" : "s"}` })),
      el("div", { class: "sources" }, b.items.map((s) => el("div", { class: "source" }, el("b", { text: `${s.document} — passage ${s.chunk}` }), el("span", { text: s.excerpt + (s.excerpt.length >= 240 ? "…" : "") })))));
  }

  // ───────────── sending ─────────────
  async function send(text) {
    text = (text ?? $("prompt").value).trim();
    if (!text || state.busy || !state.domain) return;
    state.busy = true; $("send").disabled = true;
    $("prompt").value = ""; autosize();
    let thread = $("messages").querySelector(".thread");
    if (!thread) thread = startThread();
    thread.append(el("div", { class: "msg user" }, el("div", { class: "bubble", text })));
    const pending = el("div", { class: "msg assistant" }, el("div", { class: "answer thinking", role: "status" },
      el("span", { class: "dots", "aria-hidden": "true" }, el("i"), el("i"), el("i")), el("span", { text: "Querying the database…" })));
    thread.append(pending); scrollDown();
    try {
      const res = await api("/api/chat", { method: "POST", body: { domain: state.domain.key, message: text, conversation_id: state.convId } });
      const isNew = !state.convId;
      state.convId = res.conversation_id;
      pending.replaceWith(renderMessage(res.message));
      if (isNew) await loadConversations(); else renderConvList();
    } catch (ex) {
      const retry = el("button", { class: "tiny", type: "button", text: "Try again", onclick: () => { pending.remove(); send(text); } });
      pending.replaceChildren(el("div", { class: "answer error", role: "alert" }, el("div", { class: "prose" }, el("p", { text: ex.message })), el("div", { class: "msg-tools" }, retry)));
    } finally { state.busy = false; $("send").disabled = false; scrollDown(); $("prompt").focus(); }
  }

  function autosize() { const t = $("prompt"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 160) + "px"; }

  // ───────────── settings ─────────────
  function applyTheme(choice) {
    const root = document.documentElement;
    if (choice === "light" || choice === "dark") root.setAttribute("data-theme", choice); else root.removeAttribute("data-theme");
    try { localStorage.setItem("omniagent-theme", choice); } catch { /* storage unavailable */ }
    document.querySelectorAll("[data-theme-choice]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeChoice === choice)));
  }

  async function openSettings() {
    $("settings").hidden = false; $("close-settings").focus();
    renderSchema(); if (state.config.rag_enabled) loadDocs(); if (state.user.role === "admin") loadAdmin();
  }

  async function renderSchema() {
    const box = $("schema-view"); box.replaceChildren(el("p", { class: "hint", text: "Loading…" }));
    try {
      const s = await api(`/api/domains/${encodeURIComponent(state.domain.key)}/schema`);
      box.replaceChildren(...s.tables.map((t) => el("details", {}, el("summary", { text: t.name }),
        t.description ? el("p", { class: "hint", text: t.description }) : null,
        el("ul", {}, t.columns.map((c) => el("li", { text: `${c.name} (${c.type})` }))))));
    } catch (ex) { box.replaceChildren(el("p", { class: "hint", text: ex.message })); }
  }

  async function loadDocs() {
    const list = $("doc-list");
    try {
      const docs = await api("/api/documents");
      list.replaceChildren(...docs.map((d) => el("li", {}, el("span", { text: `${d.filename} · ${d.chunks} passages` }),
        state.user.role === "viewer" ? null : el("button", { class: "tiny", type: "button", text: "Remove", onclick: async () => {
          try { await api(`/api/documents/${d.id}`, { method: "DELETE" }); loadDocs(); } catch (ex) { toast(ex.message); } } }))));
      $("doc-upload-label").hidden = state.user.role === "viewer";
    } catch (ex) { $("doc-status").textContent = ex.message; }
  }

  async function uploadDoc(e) {
    const f = e.target.files[0]; e.target.value = "";
    if (!f) return;
    const status = $("doc-status"); status.textContent = "Reading and indexing…";
    const form = new FormData(); form.append("file", f);
    try { await api("/api/documents", { method: "POST", form }); status.textContent = "Document added."; loadDocs(); }
    catch (ex) { status.textContent = ex.message; }
  }

  async function loadAdmin() {
    const box = $("admin-users");
    try {
      const data = await api("/api/admin/users");
      box.replaceChildren(...data.users.map((u) => {
        const patch = async (body) => {
          try { await api(`/api/admin/users/${u.id}`, { method: "PATCH", body }); toast("Saved"); } catch (ex) { toast(ex.message); loadAdmin(); }
        };
        const role = el("select", { "aria-label": `Role for ${u.email}`, onchange: (e) => patch({ role: e.target.value }) },
          ["admin", "analyst", "viewer"].map((r) => el("option", { value: r, text: r, selected: r === u.role })));
        const active = el("label", { class: "dom-checks" }, el("input", { type: "checkbox", checked: u.is_active, onchange: (e) => patch({ is_active: e.target.checked }) }), "Active");
        const doms = el("div", { class: "dom-checks" }, data.domains.map((d) => el("label", {},
          el("input", { type: "checkbox", checked: u.domains.includes(d.key), onchange: () => {
            const keys = [...doms.querySelectorAll("input:checked")].map((i) => i.dataset.key); patch({ domains: keys }); }, "data-key": d.key }), d.key)));
        return el("div", { class: "user-row" }, el("header", {}, el("strong", { text: `${u.display_name} · ${u.email}` }), role), active, doms);
      }));
    } catch (ex) { box.replaceChildren(el("p", { class: "hint", text: ex.message })); }
  }

  // ───────────── mobile sidebar ─────────────
  function openSidebar() { $("sidebar").classList.add("open"); $("scrim").hidden = false; $("menu").setAttribute("aria-expanded", "true"); }
  function closeSidebar() { $("sidebar").classList.remove("open"); $("scrim").hidden = true; $("menu").setAttribute("aria-expanded", "false"); }

  // ───────────── wiring ─────────────
  function bind() {
    $("auth-form").addEventListener("submit", submitAuth);
    $("auth-switch").addEventListener("click", () => setAuthMode(state.authMode === "login" ? "register" : "login"));
    $("composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
    $("prompt").addEventListener("input", autosize);
    $("prompt").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
    $("new-chat").addEventListener("click", () => { renderEmpty(); closeSidebar(); $("prompt").focus(); });
    $("domain-select").addEventListener("change", (e) => selectDomain(e.target.value));
    $("clear-chat").addEventListener("click", clearConversation);
    $("logout").addEventListener("click", async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch { /* already signed out */ } showAuth(); });
    $("open-settings").addEventListener("click", openSettings);
    $("close-settings").addEventListener("click", () => ($("settings").hidden = true));
    $("refresh-schema").addEventListener("click", async () => {
      try { await api(`/api/domains/${encodeURIComponent(state.domain.key)}/refresh-schema`, { method: "POST" }); toast("Schema refreshed"); renderSchema(); }
      catch (ex) { toast(ex.message); }
    });
    $("doc-file").addEventListener("change", uploadDoc);
    $("menu").addEventListener("click", openSidebar);
    $("scrim").addEventListener("click", closeSidebar);
    document.querySelectorAll("[data-theme-choice]").forEach((b) => b.addEventListener("click", () => applyTheme(b.dataset.themeChoice)));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("settings").hidden = true; closeSidebar(); } });
  }

  async function boot() {
    bind();
    let theme = "auto"; try { theme = localStorage.getItem("omniagent-theme") || "auto"; } catch { /* ignore */ }
    applyTheme(theme);
    setAuthMode("login");
    try { await enterApp(await api("/api/auth/me")); }
    catch { showAuth(); }
  }
  boot();
})();
