/* Session Explorer page logic: the sidebar of live conversations, the
 * Stats tab, live refresh. The transcript viewer itself is in
 * transcript_view.js, loaded before this file — see ui/session_explorer.py.
 */

/* ── State ──────────────────────────────────────────────────────────────── */
const state = {
  list: (typeof _initialList !== "undefined") ? _initialList : { mains: [], anon: [] },
  selected: null,        // conv id
  tab: "stats",
  transcript: null,      // parsed {turns, cursor}
  statsRequests: -1,     // request_count the loaded chart was built from
  anonOpen: false,
  follow: true,
  pendingNewer: false,
  hidden: new Set(["meta"]),
  matches: [], matchIdx: -1,
  observer: null,
};

/* ── Sidebar ────────────────────────────────────────────────────────────── */
function findRow(id) {
  const L = state.list;
  for (const m of L.mains || []) {
    if (m.id === id) return m;
    for (const c of m.children || []) if (c.id === id) return c;
  }
  for (const a of L.anon || []) if (a.id === id) return a;
  return null;
}

function rowEl(row, opts) {
  const d = el("div", "row" + (opts.child ? " child" : "") + (opts.dim ? " dim" : "")
    + (state.selected === row.id ? " sel" : ""));
  d.appendChild(el("div", "lbl", row.label || row.id));
  const sub = el("div", "sub");
  const bits = [];
  if (row.tokens_fmt) bits.push(row.tokens_fmt);
  if (row.cache_pct !== null && row.cache_pct !== undefined) bits.push("cache:" + row.cache_pct + "%");
  if (row.requests) bits.push("×" + row.requests);
  if (row.transcript_only) bits.push("transcript only");
  sub.appendChild(el("span", null, bits.join("  ")));
  if (row.active) sub.appendChild(el("span", "dot"));
  d.appendChild(sub);
  d.onclick = () => selectConv(row.id);
  return d;
}

function renderSidebar() {
  const box = $("rows");
  box.textContent = "";
  const L = state.list;

  if ((L.mains || []).length) {
    box.appendChild(el("div", "sec", "Conversations"));
    for (const m of L.mains) {
      box.appendChild(rowEl(m, {}));
      for (const c of m.children || []) box.appendChild(rowEl(c, { child: true }));
    }
  }
  if ((L.anon || []).length) {
    const h = el("div", "sec clickable",
      (state.anonOpen ? "▾ " : "▸ ") + "Other clients (" + L.anon.length + ")");
    h.onclick = () => { state.anonOpen = !state.anonOpen; renderSidebar(); };
    box.appendChild(h);
    if (state.anonOpen) for (const a of L.anon) box.appendChild(rowEl(a, { dim: true }));
  }
  if (!(L.mains || []).length && !(L.anon || []).length) {
    box.appendChild(el("div", "sec", "No conversations yet"));
    const hint = el("div", "row dim");
    hint.appendChild(el("div", "lbl", "Link Claude Code to the LLM proxy and start a session."));
    box.appendChild(hint);
  }

  const f = L.footer || {};
  $("foot").textContent = "";
  if (f.mains) {
    $("foot").appendChild(el("div", null,
      f.mains + " live · " + (f.agents ? f.agents + " agents · " : "") + f.tokens_fmt + " tokens"));
    if (f.agents) $("foot").appendChild(el("div", null, "+ " + f.agent_tokens_fmt + " agent tokens"));
  }
}

/* ── Selection / tabs ───────────────────────────────────────────────────── */
function selectConv(id) {
  if (state.selected === id) return;
  state.selected = id;
  state.transcript = null;
  state.statsRequests = -1;
  state.matches = []; state.matchIdx = -1; $("hits").textContent = "";
  state.pendingNewer = false;
  renderSidebar();
  renderHeader();
  loadCurrentTab(true);
}

function renderHeader() {
  const row = findRow(state.selected);
  if (!row) { $("title").textContent = "Session Explorer"; $("attr").textContent = ""; return; }
  $("title").textContent = (row.parent_id ? "⚡ " : "") + (row.label || row.id);
  const attr = $("attr");
  attr.textContent = "";
  if (row.parent_id) {
    const parent = findRow(row.parent_id);
    attr.appendChild(el("span", null, "↖ spawned by "));
    const a = el("a", null, parent ? ("“" + parent.label + "”") : row.parent_id);
    a.onclick = () => selectConv(row.parent_id);
    attr.appendChild(a);
    if (row.model) attr.appendChild(el("span", null, "  ·  " + row.model));
  } else {
    const bits = [];
    if (row.model) bits.push(row.model);
    if (row.has_transcript === false) bits.push("no transcript on disk");
    attr.textContent = bits.join("  ·  ");
  }
}

function setTab(tab) {
  state.tab = tab;
  $("tabStats").classList.toggle("sel", tab === "stats");
  $("tabExplorer").classList.toggle("sel", tab === "explorer");
  updatePanes();
  loadCurrentTab(false);
}
$("tabStats").onclick = () => setTab("stats");
$("tabExplorer").onclick = () => setTab("explorer");

function updatePanes() {
  const has = !!state.selected;
  $("statsPane").classList.toggle("sel", has && state.tab === "stats");
  $("explorerPane").classList.toggle("sel", has && state.tab === "explorer");
  $("emptyPane").classList.toggle("sel", !has);
}

function loadCurrentTab(force) {
  updatePanes();
  if (!state.selected) return;
  if (state.tab === "stats") loadStats(force);
  else loadTranscript(force);
}

/* ── Stats tab ──────────────────────────────────────────────────────────── */
let statsToken = 0;
function loadStats(force) {
  const id = state.selected;
  const row = findRow(id);
  if (!force && row && row.requests === state.statsRequests) return;
  const tok = ++statsToken;
  RPC.call("stats", { conv_id: id }).then((r) => {
    if (tok !== statsToken || state.selected !== id) return;
    if (r.html) {
      state.statsRequests = r.requests || 0;
      $("statsFrame").style.display = "";
      $("statsEmpty").style.display = "none";
      $("statsFrame").srcdoc = r.html;
    } else {
      $("statsFrame").style.display = "none";
      const em = $("statsEmpty");
      em.style.display = "flex";
      em.textContent = r.empty || r.error || "No data.";
    }
  });
}

/* Find the sidebar child conversation a Task block spawned. */
function findTaskChild(task) {
  const main = findRow(state.selected);
  const parentId = main && main.parent_id ? main.parent_id : state.selected;
  const parent = findRow(parentId);
  if (!parent || !parent.children) return null;
  const seed = (task.prompt_seed || "").slice(0, 40).toLowerCase();
  for (const c of parent.children) {
    if (seed && (c.label || "").toLowerCase().startsWith(seed.slice(0, 30))) return c.id;
  }
  if (parent.children.length === 1) return parent.children[0].id;
  return null;
}


/* ── Live updates ───────────────────────────────────────────────────────── */
let pingTimer = null;
window._voittaPing = () => {
  clearTimeout(pingTimer);
  pingTimer = setTimeout(refreshAll, 700);
};
function refreshAll() {
  RPC.call("list", {}).then((r) => {
    if (r.mains) {
      state.list = r;
      renderSidebar();
      renderHeader();
    }
    if (!state.selected) return;
    if (state.tab === "stats") loadStats(false);
    else pollTranscript();
  });
}
/* Slow heartbeat: keeps the active-dots fresh even with no traffic, and
 * catches transcript growth from sessions not proxied. */
setInterval(refreshAll, 5000);

/* ── Init ───────────────────────────────────────────────────────────────── */
renderChips();
renderSidebar();
updatePanes();
const first = (state.list.mains || [])[0];
if (first) selectConv(first.id);
