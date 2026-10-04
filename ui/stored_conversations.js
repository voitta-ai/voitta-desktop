/* Stored conversations window: the list of /voitta-store copies, a
 * conversation's full history (shared transcript viewer, transcript_view.js,
 * loaded before this file), its models and routing, export and delete.
 * Data comes over the same RPC bridge as the Session Explorer — see
 * ui/stored_conversations.py. Every stored text is untrusted and enters the
 * DOM through textContent only.
 */

const state = {
  list: (typeof _initialList !== "undefined") ? _initialList : { conversations: [] },
  selected: null,        // conv id: "<session>" or "<session>@<agent>"
  tab: "explorer",       // "explorer" (Conversation) | "models"
  transcript: null,
  routing: null,         // {session, records}
  follow: true,
  pendingNewer: false,
  hidden: new Set(["meta"]),
  matches: [], matchIdx: -1,
  observer: null,
  multi: false,          // selection mode
  checked: new Set(),    // session ids
};

const sessionOf = (id) => (id || "").split("@")[0];
const agentOf = (id) => (id || "").includes("@") ? id.split("@")[1] : null;
const conversations = () => state.list.conversations || [];
const metaOf = (sid) => conversations().find((m) => m.session_id === sid) || null;

function fmtBytes(n) {
  if (!n) return "0 B";
  if (n >= 1e9) return (n / 1e9).toFixed(1) + " GB";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
  if (n >= 1e3) return Math.round(n / 1e3) + " KB";
  return n + " B";
}
function fmtStamp(meta) {
  const d = meta.stored_ts ? new Date(meta.stored_ts * 1000) : null;
  if (!d) return meta.stored_at || "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " "
    + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
const titleOf = (m) => m.title || m.first_prompt || m.session_id;
const short = (model) => (model || "").replace(/^claude-/, "");
function topKeys(obj, n) {
  return Object.keys(obj || {}).slice(0, n);
}
function modelsLine(m) {
  const asked = topKeys((m.models || {}).requested, 2).map(short);
  const answered = topKeys((m.models || {}).answered_by, 3).map((k) => short(k.split(" @ ")[0]))
    .filter((k) => k !== "(see transcript)" && !asked.includes(k));
  return asked.join(", ") + (answered.length ? " → " + answered.join(", ") : "");
}
function searchText(m) {
  const acct = ((m.llm || {}).account || {});
  return [titleOf(m), m.first_prompt, m.project, m.cwd, m.session_id, acct.label, acct.provider,
    Object.keys((m.models || {}).requested || {}).join(" "),
    Object.keys((m.models || {}).answered_by || {}).join(" "),
    (m.subagents || []).map((a) => a.label).join(" ")].join(" ").toLowerCase();
}

/* ── Sidebar ────────────────────────────────────────────────────────────── */
function visible() {
  const q = $("search").value.trim().toLowerCase();
  const project = $("project").value;
  let rows = conversations().filter((m) =>
    (!project || m.project === project) && (!q || q.split(/\s+/).every((w) => searchText(m).includes(w))));
  const sort = $("sort").value;
  const by = {
    new: (a, b) => (b.stored_ts || 0) - (a.stored_ts || 0),
    old: (a, b) => (a.stored_ts || 0) - (b.stored_ts || 0),
    size: (a, b) => (b.bytes || 0) - (a.bytes || 0),
    title: (a, b) => titleOf(a).localeCompare(titleOf(b)),
  }[sort];
  return rows.sort(by);
}

function renderProjects() {
  const sel = $("project");
  const current = sel.value;
  const names = [...new Set(conversations().map((m) => m.project).filter(Boolean))].sort();
  sel.textContent = "";
  sel.appendChild(new Option("All projects", ""));
  for (const n of names) sel.appendChild(new Option(n, n));
  sel.value = names.includes(current) ? current : "";
}

function rowEl(m) {
  const sid = m.session_id;
  const on = state.multi ? state.checked.has(sid) : sessionOf(state.selected) === sid && !agentOf(state.selected);
  const d = el("div", "row" + (on ? " sel" : ""));
  if (state.multi) {
    const box = el("input", "chk");
    box.type = "checkbox";
    box.checked = state.checked.has(sid);
    box.onclick = (ev) => { ev.stopPropagation(); toggleCheck(sid); };
    d.appendChild(box);
  }
  const t = el("div", "rowText");
  t.appendChild(el("div", "lbl", titleOf(m)));
  const sub = el("div", "sub");
  sub.appendChild(el("span", null, [m.project, fmtStamp(m), fmtBytes(m.bytes)].filter(Boolean).join(" · ")));
  t.appendChild(sub);
  const ml = modelsLine(m);
  if (ml) t.appendChild(el("div", "sub2", ml));
  d.appendChild(t);
  d.onclick = () => (state.multi ? toggleCheck(sid) : selectConv(sid));
  return d;
}

function childRowEl(sid, agent) {
  const id = sid + "@" + agent.id;
  const d = el("div", "row child" + (state.selected === id ? " sel" : ""));
  const t = el("div", "rowText");
  t.appendChild(el("div", "lbl", agent.label || agent.id));
  t.appendChild(el("div", "sub", agent.id));
  d.appendChild(t);
  d.onclick = () => selectConv(id);
  return d;
}

function renderSidebar() {
  const box = $("rows");
  box.textContent = "";
  const rows = visible();
  if (!conversations().length) {
    box.appendChild(el("div", "empty",
      "Nothing stored yet. In Claude Code, type /voitta-store to keep the window's whole conversation here."));
  } else if (!rows.length) {
    box.appendChild(el("div", "empty", "No stored conversation matches."));
  }
  for (const m of rows) {
    box.appendChild(rowEl(m));
    if (!state.multi && sessionOf(state.selected) === m.session_id) {
      for (const a of m.subagents || []) box.appendChild(childRowEl(m.session_id, a));
    }
  }
  const total = conversations().reduce((s, m) => s + (m.bytes || 0), 0);
  $("footText").textContent = state.multi
    ? state.checked.size + " selected"
    : conversations().length + " stored · " + fmtBytes(total);
  $("selectBtn").textContent = state.multi ? "Done" : "Select";
  $("selectBtn").classList.toggle("on", state.multi);
}

function toggleCheck(sid) {
  if (state.checked.has(sid)) state.checked.delete(sid); else state.checked.add(sid);
  renderSidebar();
  renderHeader();
}

$("selectBtn").onclick = () => {
  state.multi = !state.multi;
  state.checked.clear();
  if (state.multi && state.selected) state.checked.add(sessionOf(state.selected));
  renderSidebar();
  renderHeader();
  updatePanes();
};
$("search").addEventListener("input", renderSidebar);
$("sort").onchange = renderSidebar;
$("project").onchange = renderSidebar;

/* ── Selection, header, tabs ──────────────────────────────────────────────── */
function selectConv(id) {
  if (state.selected === id) return;
  const sameSession = sessionOf(id) === sessionOf(state.selected);
  state.selected = id;
  state.transcript = null;
  if (!sameSession) state.routing = null;
  state.matches = []; state.matchIdx = -1; $("hits").textContent = ""; $("q").value = "";
  state.pendingNewer = false;
  renderSidebar();
  renderHeader();
  loadCurrentTab(true);
}

function fact(box, k, v) {
  if (!v) return;
  box.appendChild(el("div", "k", k));
  const vv = el("div", "v", v);
  vv.title = v;
  box.appendChild(vv);
}

function countsLine(obj, n) {
  const keys = Object.keys(obj || {});
  const parts = keys.slice(0, n).map((k) => short(k) + " ×" + obj[k]);
  if (keys.length > n) parts.push("+" + (keys.length - n) + " more");
  return parts.join(" · ");
}

function button(text, onclick, cls) {
  const b = el("span", "btn" + (cls ? " " + cls : ""), text);
  b.onclick = onclick;
  return b;
}

function renderHeader() {
  const facts = $("facts"), actions = $("actions"), attr = $("attr");
  facts.textContent = ""; actions.textContent = ""; attr.textContent = "";
  $("tabs").style.display = "none";

  if (state.multi) {
    const ids = [...state.checked];
    const bytes = ids.reduce((s, id) => s + ((metaOf(id) || {}).bytes || 0), 0);
    $("title").textContent = ids.length ? ids.length + " conversation" + (ids.length === 1 ? "" : "s")
      + " selected" : "Select conversations";
    attr.textContent = ids.length ? fmtBytes(bytes) : "Click rows to select them.";
    actions.appendChild(button("Select all shown", () => {
      for (const m of visible()) state.checked.add(m.session_id);
      renderSidebar(); renderHeader();
    }));
    if (ids.length) {
      actions.appendChild(button("Export ▾", (ev) => { ev.stopPropagation(); openExport(ev.currentTarget, ids); }));
      actions.appendChild(button("Delete…", () => confirmDelete(ids), "danger"));
    }
    actions.appendChild(el("span", "status", "")).id = "status";
    return;
  }

  const sid = sessionOf(state.selected);
  const m = metaOf(sid);
  if (!m) { $("title").textContent = "Stored conversations"; return; }
  const agent = agentOf(state.selected);
  $("tabs").style.display = "";
  if (agent) {
    const a = (m.subagents || []).find((x) => x.id === agent) || { label: agent };
    $("title").textContent = "⚡ " + (a.label || agent);
    attr.appendChild(el("span", null, "↖ subagent of "));
    const link = el("a", null, "“" + titleOf(m) + "”");
    link.onclick = () => selectConv(sid);
    attr.appendChild(link);
  } else {
    $("title").textContent = titleOf(m);
    const c = m.counts || {};
    attr.textContent = [m.cwd, "stored " + fmtStamp(m), fmtBytes(m.bytes),
      "Claude Code " + ((m.claude_code || {}).version || "?"),
      (c.user || 0) + " user / " + (c.assistant || 0) + " assistant records"
      + (c.subagents ? " · " + c.subagents + " subagent" + (c.subagents === 1 ? "" : "s") : "")]
      .filter(Boolean).join("  ·  ");
  }
  const acct = ((m.llm || {}).account || {});
  fact(facts, "Asked for", countsLine((m.models || {}).requested, 3));
  fact(facts, "Answered by", countsLine((m.models || {}).answered_by, 3)
    || "Voitta has no routing record for this window");
  fact(facts, "Window's LLM", (acct.label || "As is") + " (" + ((m.llm || {}).route === "window"
    ? "picked with /llm" : "default") + ")");

  actions.appendChild(button("Export ▾", (ev) => { ev.stopPropagation(); openExport(ev.currentTarget, [sid]); }));
  actions.appendChild(button("Reveal in Finder", () => RPC.call("reveal", { session: sid })));
  actions.appendChild(button("Copy session id", () => {
    RPC.call("copy", { text: sid }).then(() => status("Copied " + sid));
  }));
  actions.appendChild(button("Delete…", () => confirmDelete([sid]), "danger"));
  actions.appendChild(el("span", "status", "")).id = "status";
}

let statusTimer = null;
function status(text, isErr) {
  const s = $("status");
  if (!s) return;
  s.textContent = text;
  s.classList.toggle("err", !!isErr);
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { if ($("status")) $("status").textContent = ""; }, 6000);
}

function setTab(tab) {
  state.tab = tab;
  $("tabConversation").classList.toggle("sel", tab === "explorer");
  $("tabModels").classList.toggle("sel", tab === "models");
  loadCurrentTab(false);
}
$("tabConversation").onclick = () => setTab("explorer");
$("tabModels").onclick = () => setTab("models");

function updatePanes() {
  const has = !!state.selected && !state.multi;
  $("explorerPane").classList.toggle("sel", has && state.tab === "explorer");
  $("modelsPane").classList.toggle("sel", has && state.tab === "models");
  $("emptyPane").classList.toggle("sel", !has);
  $("emptyMsg").textContent = state.multi ? "Choose what to export or delete above."
    : conversations().length ? "Select a conversation" : "No stored conversations";
}

function loadCurrentTab(force) {
  updatePanes();
  if (!state.selected || state.multi) return;
  if (state.tab === "explorer") loadTranscript(force);
  else loadModels();
}

$("toTop").onclick = () => { state.follow = false; $("scroll").scrollTop = 0; };
$("toEnd").onclick = () => { state.follow = true; scrollBottom(); };

/* Task blocks → the subagent they spawned (matched on its first prompt). */
function findTaskChild(task) {
  const sid = sessionOf(state.selected);
  const m = metaOf(sid);
  if (!m || !(m.subagents || []).length) return null;
  const seed = (task.prompt_seed || "").slice(0, 30).toLowerCase();
  for (const a of m.subagents) {
    if (seed && (a.label || "").toLowerCase().startsWith(seed)) return sid + "@" + a.id;
  }
  return m.subagents.length === 1 ? sid + "@" + m.subagents[0].id : null;
}

/* ── Models & routing ──────────────────────────────────────────────────────── */
function table(headers, rows, rowClass) {
  const t = el("table", "grid");
  const tr = el("tr");
  for (const h of headers) tr.appendChild(el("th", null, h));
  t.appendChild(tr);
  for (const r of rows) {
    const row = el("tr", rowClass ? rowClass(r) : null);
    for (const [text, cls] of r.cells) row.appendChild(el("td", cls || null, text));
    t.appendChild(row);
  }
  return t;
}

let modelsToken = 0;
function loadModels() {
  const sid = sessionOf(state.selected);
  const pane = $("modelsPane");
  if (state.routing && state.routing.session === sid) { renderModels(); return; }
  pane.textContent = "";
  pane.appendChild(el("div", "note", "Loading…"));
  const tok = ++modelsToken;
  RPC.call("routing", { session: sid }).then((r) => {
    if (tok !== modelsToken || sessionOf(state.selected) !== sid) return;
    state.routing = { session: sid, records: r.records || [], error: r.error };
    renderModels();
  });
}

function renderModels() {
  const pane = $("modelsPane");
  pane.textContent = "";
  const m = metaOf(sessionOf(state.selected)) || {};
  const models = m.models || {};
  const acct = ((m.llm || {}).account || {});

  pane.appendChild(el("h4", null, "Asked for by Claude Code"));
  pane.appendChild(el("div", "note", "The model named in each answer Claude Code recorded (subagents included)."));
  pane.appendChild(table(["Model", "Answers"], Object.entries(models.requested || {}).map(([k, v]) =>
    ({ cells: [[k, "mono"], [String(v), "num"]] }))));

  pane.appendChild(el("h4", null, "Answered by"));
  pane.appendChild(el("div", "note", "What the provider reported for each request Voitta routed for this "
    + "window. For “As is” requests the transcript already holds Anthropic's own answer."));
  const answered = Object.entries(models.answered_by || {});
  if (answered.length) {
    pane.appendChild(table(["Model @ provider", "Requests"], answered.map(([k, v]) =>
      ({ cells: [[k, "mono"], [String(v), "num"]] }))));
  } else {
    pane.appendChild(el("div", "note", "No routing record: this window's requests were stored before "
      + "Voitta kept a routing journal, or never went through Voitta."));
  }

  pane.appendChild(el("h4", null, "This window's LLM when stored"));
  const am = acct.models || {};
  pane.appendChild(table(["Account", "Provider", "How", "Main model", "Background model"], [{ cells: [
    [acct.label || "As is"],
    [!acct.provider || acct.provider === "as_is" ? "Claude Code's own login" : acct.provider],
    [(m.llm || {}).route === "window" ? "picked with /llm" : "default"],
    [am.big || "—", "mono"], [am.small || "—", "mono"]] }]));

  pane.appendChild(el("h4", null, "Routing journal"));
  const R = state.routing || {};
  if (R.error) { pane.appendChild(el("div", "note", R.error)); return; }
  const recs = (R.records || []).slice().reverse();
  if (!recs.length) { pane.appendChild(el("div", "note", "Empty.")); return; }
  const shown = recs.slice(0, 2000);
  if (recs.length > shown.length) {
    pane.appendChild(el("div", "note", "Newest " + shown.length + " of " + recs.length + " requests."));
  }
  pane.appendChild(table(["Time", "Account", "Route", "Asked for → sent", "Answered by", "Status", "ms"],
    shown.map((r) => {
      const up = r.upstream || {};
      const when = r.ts ? new Date(r.ts * 1000).toLocaleString() : "";
      const sent = r.upstream_model && r.upstream_model !== r.model ? " → " + r.upstream_model : "";
      return {
        status: r.status,
        cells: [[when], [r.account || ""], [r.route || ""], [short(r.model) + sent, "mono"],
          [(up.model || (r.account_id === "as-is" ? "(see transcript)" : "")) + (up.host ? " @ " + up.host : ""), "mono"],
          [String(r.status ?? ""), "num"], [String(r.ms ?? ""), "num"]],
      };
    }), (r) => (r.status && r.status >= 400 ? "bad" : null)));
}

/* ── Export ────────────────────────────────────────────────────────────────── */
const FORMATS = [
  ["md", "Markdown", "Readable: turns, tool calls and results, subagents"],
  ["json", "JSON", "Everything in one file: transcript, subagents, routing, models"],
  ["jsonl", "Claude Code transcript (.jsonl)", "Claude Code's own file, byte for byte"],
];
function openExport(anchor, ids) {
  const menu = $("exportMenu");
  menu.textContent = "";
  if (ids.length > 1) menu.appendChild(el("div", "note", "  " + ids.length + " conversations → one .zip"));
  for (const [fmt, label, hint] of FORMATS) {
    const item = el("div", "item", label);
    item.appendChild(el("small", null, hint));
    item.onclick = () => { closeExport(); runExport(ids, fmt); };
    menu.appendChild(item);
  }
  const r = anchor.getBoundingClientRect();
  menu.style.left = Math.min(r.left, window.innerWidth - 280) + "px";
  menu.style.top = (r.bottom + 4) + "px";
  menu.classList.add("show");
}
function closeExport() { $("exportMenu").classList.remove("show"); }
document.addEventListener("click", (ev) => { if (!ev.target.closest("#exportMenu")) closeExport(); });

function runExport(ids, fmt) {
  status("Exporting…");
  RPC.call("export", { sessions: ids, format: fmt }).then((r) => {
    if (r.cancelled) status("");
    else if (r.error) status("Export failed: " + r.error, true);
    else status("Saved " + r.name);
  });
}

/* ── Delete ────────────────────────────────────────────────────────────────── */
function confirmDelete(ids) {
  const box = $("modalBox");
  box.textContent = "";
  box.appendChild(el("h3", null, "Delete " + ids.length + " stored conversation" + (ids.length === 1 ? "" : "s") + "?"));
  const names = el("div", "names");
  for (const id of ids) {
    const m = metaOf(id) || {};
    names.appendChild(el("div", null, "“" + titleOf(m) + "” (" + fmtBytes(m.bytes) + ")"));
  }
  box.appendChild(names);
  box.appendChild(el("p", null, "This removes Voitta's copy only. Claude Code's own transcript in "
    + "~/.claude is not touched. It cannot be undone."));
  const buttons = el("div", "buttons");
  buttons.appendChild(button("Cancel", closeModal));
  buttons.appendChild(button("Delete", () => {
    closeModal();
    RPC.call("delete", { sessions: ids }).then((r) => {
      if (r.error) { status("Delete failed: " + r.error, true); return; }
      if (ids.includes(sessionOf(state.selected))) { state.selected = null; state.transcript = null; }
      for (const id of ids) state.checked.delete(id);
      applyList(r.list);
    });
  }, "danger"));
  box.appendChild(buttons);
  $("modal").classList.add("show");
}
function closeModal() { $("modal").classList.remove("show"); }
$("modal").onclick = (ev) => { if (ev.target === $("modal")) closeModal(); };
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") { closeModal(); closeExport(); }
});

/* ── Live list ─────────────────────────────────────────────────────────────── */
function applyList(list) {
  if (!list) return;
  state.list = list;
  if (state.selected && !metaOf(sessionOf(state.selected))) { state.selected = null; state.transcript = null; }
  renderProjects();
  renderSidebar();
  renderHeader();
  updatePanes();
}
/* Python pings when a conversation is stored or deleted elsewhere. */
window._voittaPing = () => {
  RPC.call("list", {}).then((r) => {
    const fresh = r && (r.conversations || []).find((m) => m.session_id === sessionOf(state.selected));
    const old = metaOf(sessionOf(state.selected));
    applyList(r);
    if (fresh && old && fresh.stored_ts !== old.stored_ts) {   /* re-stored: reload what is shown */
      state.transcript = null; state.routing = null;
      loadCurrentTab(true);
    }
  });
};

/* ── Init ──────────────────────────────────────────────────────────────────── */
renderChips();
renderProjects();
renderSidebar();
renderHeader();
updatePanes();
const first = visible()[0];
if (first) selectConv(first.session_id);
