/* Session Explorer page logic.
 *
 * Data arrives from Python over an RPC bridge (window.webkit.messageHandlers
 * .voitta) — see ui/session_explorer.py. All transcript content is untrusted
 * text (tool results routinely contain HTML/script fragments): every piece of
 * it enters the DOM via textContent, never innerHTML.
 */
"use strict";

/* ── RPC bridge ─────────────────────────────────────────────────────────── */
const RPC = {
  seq: 0,
  pending: {},
  call(method, params) {
    return new Promise((resolve) => {
      const id = ++this.seq;
      this.pending[id] = resolve;
      try {
        window.webkit.messageHandlers.voitta.postMessage(
          JSON.stringify({ id, method, params: params || {} }));
      } catch (e) {
        delete this.pending[id];
        resolve({ error: "bridge unavailable" });
      }
    });
  },
};
window._voittaRPC = {
  resolve(id, payload) {
    const cb = RPC.pending[id];
    delete RPC.pending[id];
    if (cb) cb(payload);
  },
};

/* ── State ──────────────────────────────────────────────────────────────── */
const state = {
  list: (typeof _initialList !== "undefined") ? _initialList : { mains: [], anon: [], seeded: [] },
  selected: null,        // conv id
  tab: "stats",
  transcript: null,      // parsed {turns, cursor}
  statsRequests: -1,     // request_count the loaded chart was built from
  anonOpen: false,
  seededOpen: true,
  follow: true,
  pendingNewer: false,
  hidden: new Set(["meta"]),
  matches: [], matchIdx: -1,
  observer: null,
};

const $ = (id) => document.getElementById(id);
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function fmtK(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString();
}
function fmtDur(a, b) {
  if (!a || !b) return "";
  const s = (new Date(b) - new Date(a)) / 1000;
  if (isNaN(s) || s <= 0) return "";
  if (s < 90) return Math.round(s) + "s";
  return Math.round(s / 60) + "m";
}

/* ── Sidebar ────────────────────────────────────────────────────────────── */
function findRow(id) {
  const L = state.list;
  for (const m of L.mains || []) {
    if (m.id === id) return m;
    for (const c of m.children || []) if (c.id === id) return c;
  }
  for (const a of L.anon || []) if (a.id === id) return a;
  for (const s of L.seeded || []) if (s.id === id) return s;
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
  if (row.seeded) bits.push("previous run");
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
  if ((L.seeded || []).length) {
    const h = el("div", "sec clickable",
      (state.seededOpen ? "▾ " : "▸ ") + "Previous session (" + L.seeded.length + ")");
    h.onclick = () => { state.seededOpen = !state.seededOpen; renderSidebar(); };
    box.appendChild(h);
    if (state.seededOpen) for (const s of L.seeded) box.appendChild(rowEl(s, { dim: true }));
  }
  if (!(L.mains || []).length && !(L.anon || []).length && !(L.seeded || []).length) {
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
    if (row.seeded) bits.push("previous run — live stats unavailable");
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

/* ── Explorer tab ───────────────────────────────────────────────────────── */
let transToken = 0;
function loadTranscript(force) {
  const id = state.selected;
  if (!force && state.transcript) return;
  const tok = ++transToken;
  RPC.call("transcript", { conv_id: id }).then((r) => {
    if (tok !== transToken || state.selected !== id) return;
    if (r.turns) {
      state.transcript = r;
      renderTranscript();
      scrollBottom();
    } else {
      state.transcript = null;
      $("scroll").textContent = "";
      const ph = el("div", "placeholder", r.empty || r.error || "No transcript.");
      ph.style.height = "60vh";
      $("scroll").appendChild(ph);
    }
  });
}

function pollTranscript() {
  const id = state.selected;
  if (!id || state.tab !== "explorer" || !state.transcript) return;
  const cursor = state.transcript.cursor;
  RPC.call("transcript", { conv_id: id, cursor }).then((r) => {
    if (state.selected !== id) return;
    if (r.unchanged || !r.turns) return;
    if (state.follow && nearBottom()) {
      state.transcript = r;
      renderTranscript();
      scrollBottom();
    } else {
      state.transcript = r;
      state.pendingNewer = true;
      renderTranscript();   /* re-render preserving scroll position */
      showPill("● new activity — jump to latest");
    }
  });
}

function nearBottom() {
  const s = $("scroll");
  return s.scrollHeight - s.scrollTop - s.clientHeight < 120;
}
function scrollBottom() {
  const s = $("scroll");
  s.scrollTop = s.scrollHeight;
  hidePill();
}
function showPill(text) { const p = $("pill"); p.textContent = text; p.classList.add("show"); }
function hidePill() { $("pill").classList.remove("show"); state.pendingNewer = false; }
$("pill").onclick = () => { state.follow = true; scrollBottom(); };
$("scroll").addEventListener("scroll", () => {
  if (nearBottom()) { state.follow = true; if (state.pendingNewer) hidePill(); }
  else state.follow = false;
});

/* Rendering — turn shells first, bodies materialized near the viewport. */
function renderTranscript() {
  const s = $("scroll");
  const keepScroll = s.scrollTop;
  s.textContent = "";
  if (state.observer) state.observer.disconnect();
  state.observer = new IntersectionObserver(onTurnVisible, { root: s, rootMargin: "800px" });

  const oh = state.transcript.overhead;
  if (oh) s.appendChild(overheadSection(oh));
  if (state.transcript.note) {
    const note = el("div", "imgMeta", state.transcript.note);
    note.style.margin = "10px 0";
    s.appendChild(note);
  }

  const turns = state.transcript.turns || [];
  for (const turn of turns) {
    s.appendChild(turnSep(turn));
    if (turn.compact || !turn.entries.length) continue;
    const body = el("div", "turn collapsedBody");
    body._turn = turn;
    body.style.minHeight = Math.min(4000, 24 * turn.entries.length) + "px";
    s.appendChild(body);
    state.observer.observe(body);
  }
  s.scrollTop = keepScroll;
  applyFilters();
}

/* Context overhead — full system prompt + tool definitions, from the
 * proxy's copy of the latest request body (not in the transcript). */
function overheadSection(oh) {
  const wrap = el("div");
  const sep = el("div", "turnSep");
  sep.appendChild(el("b", null, "CONTEXT"));
  sep.appendChild(el("span", null,
    "system " + fmtK(oh.system_chars || 0) + " · tools " +
    (oh.tools || []).length + " / " + fmtK(oh.tools_chars || 0) + " chars"));
  wrap.appendChild(sep);

  if ((oh.system || []).length) {
    wrap.appendChild(collapsible("tool", [
      el("span", "nm", "📋 system prompt"),
      el("span", "pv", "· " + oh.system.length + " block" +
        (oh.system.length !== 1 ? "s" : "") + " · " + fmtK(oh.system_chars) + " chars"),
    ], (body) => {
      for (const b of oh.system) {
        if (b.cache) body.appendChild(el("div", "imgMeta", "▼ cache_control breakpoint"));
        body.appendChild(el("pre", "mono", b.text + (b.chars > b.text.length
          ? "\n…[+" + fmtK(b.chars - b.text.length) + " chars]" : "")));
      }
    }, false));
  }

  if ((oh.tools || []).length) {
    wrap.appendChild(collapsible("tool", [
      el("span", "nm", "🧰 tools"),
      el("span", "pv", "· " + oh.tools.length + " definitions · " +
        fmtK(oh.tools_chars) + " chars"),
    ], (body) => {
      for (const t of oh.tools) {
        body.appendChild(collapsible("tool", [
          el("span", "nm", t.name),
          el("span", "pv", "· desc " + fmtK(t.desc_chars) + " · schema " +
            fmtK(t.schema_chars) + (t.cache ? " · ▼ cache" : "")),
        ], (inner) => {
          inner.appendChild(el("pre", "mono", t.desc || "(no description)"));
        }, false));
      }
    }, false));
  }
  return wrap;
}

function turnSep(turn) {
  const d = el("div", "turnSep" + (turn.compact ? " compact" : ""));
  if (turn.compact) {
    d.appendChild(el("span", null, "⇢ context compacted " + fmtTime(turn.ts_start)));
    return d;
  }
  const b = el("b", null, "TURN #" + turn.index);
  d.appendChild(b);
  const bits = [];
  const u = turn.usage;
  if (u) {
    const ctx = (u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0);
    if (ctx) bits.push("in " + fmtK(ctx));
    if (u.output_tokens) bits.push("out " + fmtK(u.output_tokens));
    if (ctx && u.cache_read_input_tokens) {
      bits.push("cache " + Math.round(u.cache_read_input_tokens * 100 / ctx) + "%");
    }
  }
  const dur = fmtDur(turn.ts_start, turn.ts_end);
  if (dur) bits.push(dur);
  if (turn.ts_start) bits.push(fmtTime(turn.ts_start));
  if (bits.length) d.appendChild(el("span", null, bits.join(" · ")));
  return d;
}

function onTurnVisible(entries) {
  for (const io of entries) {
    if (!io.isIntersecting) continue;
    const box = io.target;
    if (!box._turn) continue;
    materializeTurn(box);
  }
}

function materializeTurn(box) {
  const turn = box._turn;
  if (!turn) return;
  box._turn = null;
  box.classList.remove("collapsedBody");
  box.style.minHeight = "";
  state.observer.unobserve(box);
  for (const entry of turn.entries) box.appendChild(entryEl(entry));
}

/* One transcript entry → DOM. All content via textContent. */
function entryEl(entry) {
  const isUser = entry.role === "user";
  const wrap = el("div", "entry " + (isUser ? "userEntry" : "asstEntry")
    + (entry.meta ? " meta" : ""));
  wrap.dataset.uuid = entry.uuid;

  const hasPlainText = entry.blocks.some((b) => b.t === "text");
  let host = wrap;
  if (isUser && hasPlainText && !entry.meta) {
    host = el("div", "userCard");
    wrap.appendChild(host);
    host.appendChild(entryHead("👤 USER", entry));
  } else if (!isUser && hasPlainText) {
    host.appendChild(entryHead("🤖 ASSISTANT", entry));
  }

  for (const b of entry.blocks) host.appendChild(blockEl(b, entry));
  return wrap;
}

function entryHead(who, entry) {
  const h = el("div", "eHead");
  h.appendChild(el("span", "who", who));
  h.appendChild(el("span", "ts", fmtTime(entry.ts)));
  h.appendChild(jsonBtn(entry));
  return h;
}

function jsonBtn(entry) {
  const j = el("span", "jsonBtn", "{json}");
  j.onclick = (ev) => {
    ev.stopPropagation();
    RPC.call("raw", { conv_id: state.selected, uuid: entry.uuid }).then((r) => {
      openOverlayPre(r.json || r.error || "");
    });
  };
  return j;
}

function collapsible(cls, headParts, buildBody, startOpen) {
  const box = el("div", cls + (startOpen ? " open" : ""));
  const crow = el("div", "crow");
  const arrow = el("span", "arrow", startOpen ? "▽" : "▷");
  crow.appendChild(arrow);
  for (const p of headParts) crow.appendChild(p);
  box.appendChild(crow);
  const body = el("div", "body");
  box.appendChild(body);
  let built = false;
  const ensure = () => { if (!built) { built = true; buildBody(body); } };
  if (startOpen) ensure();
  crow.onclick = () => {
    const open = box.classList.toggle("open");
    arrow.textContent = open ? "▽" : "▷";
    if (open) ensure();
  };
  return box;
}

function blockEl(b, entry) {
  if (b.t === "text") {
    const t = el("div", "txt", b.text + (b.chars > b.text.length
      ? "\n…[+" + fmtK(b.chars - b.text.length) + " chars — {json} for full]" : ""));
    return t;
  }

  if (b.t === "thinking") {
    return collapsible("think", [
      el("span", "nm", "💭 thinking"),
      el("span", "pv", "· " + fmtK(b.chars) + " chars"),
    ], (body) => {
      const pre = el("pre", "mono", b.text);
      body.appendChild(pre);
    }, false);
  }

  if (b.t === "tool_use") {
    const parts = [el("span", "nm", "🔧 " + b.name)];
    if (b.preview) parts.push(el("span", "pv", "(" + b.preview + ")"));
    const box = collapsible("tool", parts, (body) => {
      body.appendChild(el("pre", "mono", b.input + (b.chars > b.input.length
        ? "\n…[+" + fmtK(b.chars - b.input.length) + " chars]" : "")));
    }, false);
    if (b.task) {
      const row = box.querySelector(".crow");
      row.querySelector(".nm").textContent = "⚡ Task";
      const info = b.task.description || b.task.subagent_type || "";
      if (info) row.appendChild(el("span", "pv", "→ " + info));
      const link = findTaskChild(b.task);
      if (link) {
        const taskRow = el("span", "taskRow");
        const a = el("a", null, " open ↗");
        a.onclick = (ev) => { ev.stopPropagation(); selectConv(link); };
        taskRow.appendChild(a);
        row.appendChild(taskRow);
      }
    }
    return box;
  }

  if (b.t === "tool_result") {
    const parts = [
      el("span", "nm", "↳ result"),
      el("span", "pv", "· " + fmtK(b.chars) + " chars" + (b.error ? " · ERROR" : "")),
    ];
    const box = collapsible("result" + (b.error ? " err" : ""), parts, (body) => {
      if (b.text) body.appendChild(el("pre", "mono", b.text + (b.chars > b.text.length
        ? "\n…[+" + fmtK(b.chars - b.text.length) + " chars]" : "")));
      for (const img of b.images || []) body.appendChild(imageEl(img, entry));
    }, false);
    if (b.error) box.querySelector(".crow").classList.add("err");
    return box;
  }

  if (b.t === "image") return imageEl(b, entry);
  return el("div");
}

function imageEl(b, entry) {
  const wrap = el("div");
  if (b.thumb) {
    const img = el("img", "thumb");
    img.src = "data:image/jpeg;base64," + b.thumb;
    img.onclick = () => {
      RPC.call("image", { conv_id: state.selected, uuid: entry.uuid, seq: b.seq })
        .then((r) => {
          if (r.data) openOverlayImg(r.media_type, r.data);
        });
    };
    wrap.appendChild(img);
  }
  wrap.appendChild(el("div", "imgMeta",
    b.media_type + (b.w ? " " + b.w + "×" + b.h : "") + " · " + fmtK(b.bytes) + "B"
    + (b.thumb ? "  (click for full size)" : "")));
  return wrap;
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

/* ── Overlay (image / raw JSON) ─────────────────────────────────────────── */
function openOverlayImg(mediaType, data) {
  const box = $("overlayBox");
  box.textContent = "";
  const img = el("img");
  img.src = "data:" + (mediaType || "image/png") + ";base64," + data;
  box.appendChild(img);
  $("overlay").classList.add("show");
}
function openOverlayPre(text) {
  const box = $("overlayBox");
  box.textContent = "";
  const pre = el("pre", "mono", text);
  pre.style.maxHeight = "82vh";
  pre.style.maxWidth = "82vw";
  box.appendChild(pre);
  $("overlay").classList.add("show");
}
$("overlay").onclick = (ev) => {
  if (ev.target === $("overlay")) $("overlay").classList.remove("show");
};
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") $("overlay").classList.remove("show");
});

/* ── Filters ────────────────────────────────────────────────────────────── */
const CHIPS = [
  ["user", "user"], ["assistant", "asst"], ["thinking", "think"],
  ["tools", "tools"], ["results", "results"], ["meta", "meta"],
];
function renderChips() {
  const box = $("chips");
  box.textContent = "";
  for (const [key, label] of CHIPS) {
    const c = el("span", "chip" + (state.hidden.has(key) ? "" : " on"), label);
    c.onclick = () => {
      if (state.hidden.has(key)) state.hidden.delete(key); else state.hidden.add(key);
      renderChips();
      applyFilters();
    };
    box.appendChild(c);
  }
}
function applyFilters() {
  const s = $("scroll");
  s.classList.toggle("hide-user", state.hidden.has("user"));
  s.classList.toggle("hide-assistant", state.hidden.has("assistant"));
  s.classList.toggle("hide-thinking", state.hidden.has("thinking"));
  s.classList.toggle("hide-tools", state.hidden.has("tools"));
  s.classList.toggle("hide-results", state.hidden.has("results"));
  s.classList.toggle("hide-meta", state.hidden.has("meta"));
}

/* ── Search ─────────────────────────────────────────────────────────────── */
function runSearch() {
  const q = $("q").value.trim().toLowerCase();
  state.matches = []; state.matchIdx = -1;
  document.querySelectorAll(".entry.hl").forEach((n) => n.classList.remove("hl"));
  if (!q || !state.transcript) { $("hits").textContent = ""; return; }
  const turns = state.transcript.turns || [];
  turns.forEach((turn, ti) => {
    (turn.entries || []).forEach((entry) => {
      const hit = (entry.blocks || []).some((b) =>
        (b.text && b.text.toLowerCase().includes(q)) ||
        (b.name && b.name.toLowerCase().includes(q)) ||
        (b.preview && b.preview.toLowerCase().includes(q)) ||
        (b.input && b.input.toLowerCase().includes(q)));
      if (hit) state.matches.push({ ti, uuid: entry.uuid });
    });
  });
  $("hits").textContent = state.matches.length ? "0/" + state.matches.length : "0 hits";
  if (state.matches.length) gotoMatch(0);
}
function gotoMatch(idx) {
  if (!state.matches.length) return;
  state.matchIdx = ((idx % state.matches.length) + state.matches.length) % state.matches.length;
  const m = state.matches[state.matchIdx];
  $("hits").textContent = (state.matchIdx + 1) + "/" + state.matches.length;
  state.follow = false;

  /* Materialize every turn up to and including the target so the anchor exists. */
  const boxes = document.querySelectorAll("#scroll .turn");
  boxes.forEach((box) => { if (box._turn && box._turn.index <= m.ti + 2) materializeTurn(box); });

  document.querySelectorAll(".entry.hl").forEach((n) => n.classList.remove("hl"));
  const target = document.querySelector('.entry[data-uuid="' + m.uuid + '"]');
  if (target) {
    target.classList.add("hl");
    target.scrollIntoView({ block: "center" });
  }
}
$("q").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") { if (state.matches.length) gotoMatch(state.matchIdx + 1); else runSearch(); }
});
$("q").addEventListener("input", () => { runSearch(); });
$("nextHit").onclick = () => gotoMatch(state.matchIdx + 1);
$("prevHit").onclick = () => gotoMatch(state.matchIdx - 1);

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
/* Slow heartbeat: keeps the active-dots and seeded list fresh even with no
 * traffic, and catches transcript growth from sessions not proxied. */
setInterval(refreshAll, 5000);

/* ── Init ───────────────────────────────────────────────────────────────── */
renderChips();
renderSidebar();
updatePanes();
const first = (state.list.mains || [])[0] || (state.list.seeded || [])[0];
if (first) selectConv(first.id);
