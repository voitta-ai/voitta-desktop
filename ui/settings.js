let config = (typeof _initialConfig !== 'undefined') ? _initialConfig : { apps: [], jira: {}, mcp_proxy: {}, llm_proxy: {} };

function uuid() {
  return crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2);
}

function renderApps() {
  const c = document.getElementById('apps-container');
  c.innerHTML = '';
  config.apps.forEach((app, i) => {
    const isMicrosoft = app.type === 'microsoft';
    c.innerHTML += `
      <div class="card" data-index="${i}">
        <div class="card-header">
          <input type="text" value="${esc(app.name)}" onchange="config.apps[${i}].name=this.value">
          <select onchange="typeChanged(${i}, this.value)">
            <option value="microsoft" ${isMicrosoft ? 'selected' : ''}>Microsoft</option>
            <option value="google" ${!isMicrosoft ? 'selected' : ''}>Google</option>
          </select>
        </div>
        ${isMicrosoft ? `
        <div class="field">
          <label>Tenant ID</label>
          <input type="text" value="${esc(app.tenant_id || '')}" onchange="config.apps[${i}].tenant_id=this.value">
        </div>` : ''}
        <div class="field">
          <label>Client ID</label>
          <input type="text" value="${esc(app.client_id || '')}" onchange="config.apps[${i}].client_id=this.value">
        </div>
        ${!isMicrosoft ? `
        <div class="field">
          <label>Secret</label>
          <input type="password" value="${esc(app.client_secret || '')}" onchange="config.apps[${i}].client_secret=this.value">
        </div>` : ''}
        <div class="field">
          <label>Use for</label>
          <div class="checkboxes">
            <label><input type="checkbox" ${app.use_for.includes('rag') ? 'checked' : ''}
              onchange="toggleUseFor(${i}, 'rag', this.checked)"> RAG</label>
            ${!isMicrosoft ? `<label><input type="checkbox" ${app.use_for.includes('google_workspace') ? 'checked' : ''}
              onchange="toggleUseFor(${i}, 'google_workspace', this.checked)"> Google Workspace</label>` : ''}
          </div>
        </div>
        <div class="card-footer">
          <button class="btn btn-danger" onclick="deleteApp(${i})">Delete</button>
        </div>
      </div>`;
  });
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function typeChanged(i, type) {
  config.apps[i].type = type;
  if (type === 'microsoft') {
    delete config.apps[i].client_secret;
    config.apps[i].tenant_id = config.apps[i].tenant_id || '';
    config.apps[i].use_for = config.apps[i].use_for.filter(x => x !== 'google_workspace');
  } else {
    delete config.apps[i].tenant_id;
    config.apps[i].client_secret = config.apps[i].client_secret || '';
  }
  renderApps();
}

function toggleUseFor(i, backend, checked) {
  const uf = config.apps[i].use_for;
  if (checked && !uf.includes(backend)) uf.push(backend);
  if (!checked) config.apps[i].use_for = uf.filter(x => x !== backend);
}

function addApp() {
  config.apps.push({
    id: uuid(),
    name: 'New Application',
    type: 'google',
    client_id: '',
    client_secret: '',
    use_for: []
  });
  renderApps();
}

function deleteApp(i) {
  const app = config.apps[i];
  const name = app.name || 'Unnamed';
  const typed = prompt('To delete "' + name + '", type its name below:');
  if (typed === null) return;
  if (typed.trim() !== name.trim()) {
    alert('Name does not match. Deletion cancelled.');
    return;
  }
  config.apps.splice(i, 1);
  renderApps();
}

function loadJira() {
  document.getElementById('jira_url').value = config.jira.server_url || '';
  document.getElementById('jira_email').value = config.jira.email || '';
  document.getElementById('jira_api_token').value = config.jira.api_token || '';

  // Attach change listeners to trigger project fetch
  ['jira_url', 'jira_email', 'jira_api_token'].forEach(id => {
    const el = document.getElementById(id);
    el.addEventListener('change', maybeLoadProjects);
    el.addEventListener('blur', maybeLoadProjects);
  });

  // Load projects if credentials already present
  maybeLoadProjects();
}

var _lastFetchKey = '';
function maybeLoadProjects() {
  const url = document.getElementById('jira_url').value.trim();
  const email = document.getElementById('jira_email').value.trim();
  const token = document.getElementById('jira_api_token').value.trim();
  const key = url + '|' + email + '|' + token;
  if (!url || !email || !token) return;
  if (key === _lastFetchKey) return;
  _lastFetchKey = key;
  document.getElementById('jira_project_status').textContent = 'Loading\u2026';
  // Signal Python to fetch projects via title KVO
  document.title = 'VOITTA_FETCH_JIRA_PROJECTS:' + btoa(unescape(encodeURIComponent(key)));
}

// Called from Python after fetching projects
function _setJiraProjects(projects) {
  const sel = document.getElementById('jira_project');
  const saved = (config.jira.project || '').split(',').map(s => s.trim()).filter(Boolean);
  sel.innerHTML = '';
  projects.forEach(function(p) {
    const opt = document.createElement('option');
    opt.value = p[0];
    opt.textContent = p[0] + ' \u2014 ' + p[1];
    if (saved.includes(p[0])) opt.selected = true;
    sel.appendChild(opt);
  });
  const status = document.getElementById('jira_project_status');
  status.textContent = projects.length ? projects.length + ' found' : 'None found';
}

function _setJiraProjectsError(msg) {
  document.getElementById('jira_project_status').textContent = msg;
}

function loadProxy() {
  var mcp = config.mcp_proxy || config.proxy || {};
  var llm = config.llm_proxy || {};
  var oauth = config.oauth || {};
  var sub = config.mcp_subprocess || {};
  document.getElementById('proxy_port').value = mcp.port || '18765';
  document.getElementById('edit_proxy_url').value = mcp.edit_proxy_url || '';
  document.getElementById('rag_url').value = mcp.rag_url || '';
  document.getElementById('image_rag_url').value = mcp.image_rag_url || 'https://rag-img.voitta.ai/mcp';
  document.getElementById('image_rag_key').value = mcp.image_rag_key || '';
  document.getElementById('paperclip_url').value = mcp.paperclip_url || 'https://paperclip.gxl.ai/mcp';
  document.getElementById('paperclip_key').value = mcp.paperclip_key || '';
  document.getElementById('freecad_url').value = mcp.freecad_url || 'http://127.0.0.1:50005/mcp';
  document.getElementById('llm_proxy_port').value = llm.port || '18900';
  document.getElementById('llm_upstream_url').value = llm.upstream_url || 'https://api.anthropic.com';
  document.getElementById('oauth_redirect_port').value = oauth.redirect_port || '53214';
  document.getElementById('google_mcp_port').value = sub.google_mcp_port || '18766';
  document.getElementById('google_mcp_dir').value = sub.google_mcp_dir || '~/DEVEL/google_workspace_mcp';
  document.getElementById('google_mcp_env_path').value = sub.google_mcp_env_path || '~/DEVEL/google_workspace_mcp/.env';
  document.getElementById('jira_mcp_port').value = sub.jira_mcp_port || '18767';
  document.getElementById('jira_mcp_dir').value = sub.jira_mcp_dir || '~/DEVEL/mcp-atlassian';
  document.getElementById('jira_mcp_env_path').value = sub.jira_mcp_env_path || '~/.voitta_desktop/jira.env';

  var opt = config.optimizer || {};
  document.getElementById('optimizer_enabled').checked = opt.enabled !== false;  // default true
  document.getElementById('optimizer_haiku_only').checked = !!opt.haiku_only;

  var bash = config.bash || {};
  document.getElementById('bash_strip_ansi').checked = bash.strip_ansi !== false;       // default true
  document.getElementById('bash_trim_whitespace').checked = bash.trim_whitespace !== false; // default true
  document.getElementById('bash_strip_progress').checked = !!bash.strip_progress;
  document.getElementById('bash_smart_commands').checked = !!bash.smart_commands;

  var time = config.time || {};
  document.getElementById('tool_result_keep_turns').value = time.tool_result_keep_turns || 5;
  document.getElementById('image_keep_turns').value       = time.image_keep_turns       || 5;
  document.getElementById('thinking_keep_turns').value    = time.thinking_keep_turns    || 5;

  var tools = config.tools || {};
  // Default true — Codex signs its handshake, no need to confirm every time.
  document.getElementById('suppress_codex_popup').checked = tools.suppress_codex_popup !== false;
}

function collectAll() {
  // Re-read input values that might have changed without onchange firing
  const selProj = document.getElementById('jira_project');
  const selectedProjects = Array.from(selProj.selectedOptions).map(o => o.value).join(',');
  config.jira = {
    server_url: document.getElementById('jira_url').value,
    email: document.getElementById('jira_email').value,
    api_token: document.getElementById('jira_api_token').value,
    project: selectedProjects,
  };
  config.mcp_proxy = {
    port: parseInt(document.getElementById('proxy_port').value) || 18765,
    edit_proxy_url: document.getElementById('edit_proxy_url').value,
    rag_url: document.getElementById('rag_url').value,
    image_rag_url: document.getElementById('image_rag_url').value,
    image_rag_key: document.getElementById('image_rag_key').value,
    paperclip_url: document.getElementById('paperclip_url').value,
    paperclip_key: document.getElementById('paperclip_key').value,
    freecad_url: document.getElementById('freecad_url').value,
  };
  config.llm_proxy = {
    port: parseInt(document.getElementById('llm_proxy_port').value) || 18900,
    upstream_url: document.getElementById('llm_upstream_url').value.trim() || 'https://api.anthropic.com',
  };
  config.oauth = {
    redirect_port: parseInt(document.getElementById('oauth_redirect_port').value) || 53214,
  };
  config.mcp_subprocess = {
    google_mcp_port: parseInt(document.getElementById('google_mcp_port').value) || 18766,
    google_mcp_dir: document.getElementById('google_mcp_dir').value.trim() || '~/DEVEL/google_workspace_mcp',
    google_mcp_env_path: document.getElementById('google_mcp_env_path').value.trim() || '~/DEVEL/google_workspace_mcp/.env',
    jira_mcp_port: parseInt(document.getElementById('jira_mcp_port').value) || 18767,
    jira_mcp_dir: document.getElementById('jira_mcp_dir').value.trim() || '~/DEVEL/mcp-atlassian',
    jira_mcp_env_path: document.getElementById('jira_mcp_env_path').value.trim() || '~/.voitta_desktop/jira.env',
  };
  config.optimizer = {
    enabled: document.getElementById('optimizer_enabled').checked,
    haiku_only: document.getElementById('optimizer_haiku_only').checked,
  };
  config.bash = {
    strip_ansi: document.getElementById('bash_strip_ansi').checked,
    trim_whitespace: document.getElementById('bash_trim_whitespace').checked,
    strip_progress: document.getElementById('bash_strip_progress').checked,
    smart_commands: document.getElementById('bash_smart_commands').checked,
  };
  config.time = {
    tool_result_keep_turns: Math.max(1, parseInt(document.getElementById('tool_result_keep_turns').value) || 5),
    image_keep_turns:       Math.max(1, parseInt(document.getElementById('image_keep_turns').value)       || 5),
    thinking_keep_turns:    Math.max(1, parseInt(document.getElementById('thinking_keep_turns').value)    || 5),
  };
  config.tools = {
    suppress_codex_popup: document.getElementById('suppress_codex_popup').checked,
  };
  config.disabled_tools = collectDisabledTools();
  return config;
}

// ── MCP Tools tree ──────────────────────────────────────────────────────────

var _disabledSet = new Set((config.disabled_tools || []));
var _toolGroups = (typeof _toolTree !== 'undefined') ? _toolTree : [];

function collectDisabledTools() {
  return Array.from(_disabledSet).sort();
}

function renderToolTree() {
  var container = document.getElementById('tool-tree');
  container.innerHTML = '';
  if (!_toolGroups.length) {
    container.innerHTML = '<li class="tool-empty">Loading tools\u2026</li>';
    return;
  }
  _toolGroups.forEach(function(group) {
    var li = document.createElement('li');
    li.className = 'tool-group';
    li.dataset.prefix = group.prefix;

    var header = document.createElement('div');
    header.className = 'tool-group-header';

    var arrow = document.createElement('span');
    arrow.className = 'tool-group-arrow';
    arrow.textContent = '\u25B6';

    var toggle = document.createElement('span');
    toggle.className = 'toggle';
    updateGroupToggle(toggle, group);

    var label = document.createElement('span');
    label.className = 'tool-group-label';
    label.textContent = group.label;

    var count = document.createElement('span');
    count.className = 'tool-group-count';
    var enabledCount = group.tools.filter(function(t) { return !_disabledSet.has(t); }).length;
    count.textContent = enabledCount + '/' + group.tools.length;

    toggle.onclick = function(e) {
      e.stopPropagation();
      toggleGroup(group, toggle, count);
    };

    header.appendChild(arrow);
    header.appendChild(toggle);
    header.appendChild(label);
    header.appendChild(count);

    header.onclick = function() {
      arrow.classList.toggle('open');
      childList.classList.toggle('open');
    };

    var childList = document.createElement('ul');
    childList.className = 'tool-children';

    group.tools.forEach(function(toolName) {
      var tli = document.createElement('li');
      tli.className = 'tool-item';
      tli.dataset.tool = toolName;

      var tToggle = document.createElement('span');
      tToggle.className = 'toggle' + (_disabledSet.has(toolName) ? '' : ' on');

      var tLabel = document.createElement('span');
      tLabel.textContent = toolName;

      tToggle.onclick = function() {
        if (_disabledSet.has(toolName)) {
          _disabledSet.delete(toolName);
          tToggle.className = 'toggle on';
        } else {
          _disabledSet.add(toolName);
          tToggle.className = 'toggle';
        }
        updateGroupToggle(toggle, group);
        updateGroupCount(count, group);
      };

      tli.appendChild(tToggle);
      tli.appendChild(tLabel);
      childList.appendChild(tli);
    });

    li.appendChild(header);
    li.appendChild(childList);
    container.appendChild(li);
  });
}

function updateGroupToggle(el, group) {
  var disabledCount = group.tools.filter(function(t) { return _disabledSet.has(t); }).length;
  if (disabledCount === 0) {
    el.className = 'toggle on';
  } else if (disabledCount === group.tools.length) {
    el.className = 'toggle';
  } else {
    el.className = 'toggle partial';
  }
}

function updateGroupCount(el, group) {
  var enabledCount = group.tools.filter(function(t) { return !_disabledSet.has(t); }).length;
  el.textContent = enabledCount + '/' + group.tools.length;
}

function toggleGroup(group, toggleEl, countEl) {
  var disabledCount = group.tools.filter(function(t) { return _disabledSet.has(t); }).length;
  var enableAll = disabledCount > 0;
  group.tools.forEach(function(t) {
    if (enableAll) {
      _disabledSet.delete(t);
    } else {
      _disabledSet.add(t);
    }
  });
  updateGroupToggle(toggleEl, group);
  updateGroupCount(countEl, group);
  // Update child toggles
  var groupEl = toggleEl.closest('.tool-group');
  groupEl.querySelectorAll('.tool-item .toggle').forEach(function(childToggle) {
    var name = childToggle.parentElement.dataset.tool;
    childToggle.className = 'toggle' + (_disabledSet.has(name) ? '' : ' on');
  });
}

function filterTools(query) {
  query = query.toLowerCase();
  document.querySelectorAll('.tool-group').forEach(function(group) {
    var children = group.querySelector('.tool-children');
    var arrow = group.querySelector('.tool-group-arrow');
    var items = children.querySelectorAll('.tool-item');
    var anyVisible = false;
    items.forEach(function(item) {
      var match = !query || item.dataset.tool.toLowerCase().indexOf(query) !== -1;
      item.style.display = match ? '' : 'none';
      if (match) anyVisible = true;
    });
    group.style.display = anyVisible ? '' : 'none';
    if (query && anyVisible) {
      children.classList.add('open');
      arrow.classList.add('open');
    } else if (!query) {
      children.classList.remove('open');
      arrow.classList.remove('open');
    }
  });
}

function save() {
  // Signal Python via document.title (KVO observed, data read via evaluateJavaScript)
  document.title = 'VOITTA_SAVE';
}

function cancel() {
  document.title = 'VOITTA_CANCEL';
}

// Tab switching — also reveals the Connect/Disconnect button only when
// the Proxies tab is active (it's bottom-left, intentionally tied to that
// section since the LLM proxy port is what gets wired into Claude Code).
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(function(t) {
    t.classList.toggle('active', t.dataset.tab === name);
  });
  document.querySelectorAll('.tab-pane').forEach(function(p) {
    p.classList.toggle('active', p.id === 'tab-' + name);
  });
  var linkBtn = document.getElementById('claude-link-btn');
  if (linkBtn) linkBtn.style.display = (name === 'proxies') ? '' : 'none';
}

// Connect/Disconnect button — fires a salt-prefixed title so KVO observes
// the change even when the button is clicked twice in succession.
function toggleClaudeLink() {
  var salt = Math.random().toString(36).slice(2);
  document.title = 'VOITTA_CLAUDE_LINK_TOGGLE:' + salt;
}

// Called from Python after the link state changes (or at popup open).
// `linked` is a boolean from is_voitta_connected().
function _setClaudeLinkState(linked) {
  var btn = document.getElementById('claude-link-btn');
  if (!btn) return;
  btn.textContent = linked ? 'Disconnect Claude' : 'Connect Claude';
}

// ── Info tab rendering ────────────────────────────────────────────────────
// Called from Python with a state object every ~3s while the settings
// window is open. Idempotent — safe to call repeatedly with the same data.
function _setInfoState(s) {
  if (!s) return;

  var SVG_NS = 'http://www.w3.org/2000/svg';

  // ── Status pills (top right) ───────────────────────────────────────────
  setPill('info-llm-pill', s.llm_wired ? '✓ WIRED' : '✗ OFF', s.llm_wired ? 'ok' : 'off');
  setPill('info-mcp-claude-pill',
          s.mcp_wired_claude ? '✓ WIRED' : '✗ OFF',
          s.mcp_wired_claude ? 'ok' : 'off');
  setPill('info-mcp-codex-pill',
          s.mcp_wired_codex ? '✓ WIRED' : '✗ OFF',
          s.mcp_wired_codex ? 'ok' : 'off');
  var model = (s.current_model || '—').toUpperCase();
  setPill('info-model-pill', model, 'neutral');

  // ── Conversations subtext beneath Claude Code box ──────────────────────
  var sub = document.getElementById('info-claude-sub');
  if (sub) {
    var n = s.active_conversations || 0;
    sub.textContent = n === 0 ? '' : (n === 1 ? '1 conversation' : n + ' conversations');
  }

  // ── Component dimming (broken lane = dim its destination chain) ────────
  setRectDim('info-llm-rect', !s.llm_wired);
  setRectDim('info-upstream-rect', !s.llm_wired);
  setRectDim('info-mcp-rect', !s.mcp_wired);
  setRectDim('info-backends-rect', !s.mcp_wired);
  setIconState('info-llm-icon', s.llm_wired);
  setMcpIconState('info-mcp-icon', s.mcp_wired);

  // ── Arrows: live (animated yellow) when lane is wired, dim otherwise ──
  setArrowLive('info-arrow-llm', s.llm_wired);
  setArrowLive('info-arrow-mcp', s.mcp_wired);
  setArrowLive('info-arrow-upstream', s.llm_wired);
  setArrowLive('info-arrow-backends', s.mcp_wired);
  setArrowTipColor('info-arrow-llm-tip', s.llm_wired);
  setArrowTipColor('info-arrow-mcp-tip', s.mcp_wired);
  setArrowTipColor('info-arrow-upstream-tip', s.llm_wired);
  setArrowTipColor('info-arrow-backends-tip', s.mcp_wired);

  // ── Red-X overlays on broken arrows ────────────────────────────────────
  document.getElementById('info-x-llm').style.display = s.llm_wired ? 'none' : '';
  document.getElementById('info-x-mcp').style.display = s.mcp_wired ? 'none' : '';

  // ── Upstream host text ─────────────────────────────────────────────────
  var host = document.getElementById('info-upstream-host');
  if (host) host.textContent = s.upstream_host || 'api.anthropic.com';

  // ── Backends: row of dots + summary ────────────────────────────────────
  var dotsEl = document.getElementById('info-backend-dots');
  if (dotsEl) {
    while (dotsEl.firstChild) dotsEl.removeChild(dotsEl.firstChild);
    var backends = s.backends || [];
    var max = Math.min(backends.length, 8);
    var spacing = 22;
    for (var i = 0; i < max; i++) {
      var b = backends[i];
      var dot = document.createElementNS(SVG_NS, 'circle');
      dot.setAttribute('cx', String(i * spacing));
      dot.setAttribute('cy', '0');
      dot.setAttribute('r', '5');
      var stateCls = (b.state === 'ok') ? 'ok' : (b.state === 'error' ? 'error' : 'empty');
      dot.setAttribute('class', 'backend-dot ' + stateCls);
      // Tooltip via SVG <title> for hover
      var tip = document.createElementNS(SVG_NS, 'title');
      tip.textContent = b.label + ' — ' + (b.state === 'ok' ? (b.tools_count + ' tools') : b.state);
      dot.appendChild(tip);
      dotsEl.appendChild(dot);
    }
  }
  var summary = document.getElementById('info-backends-summary');
  if (summary) {
    var ok = (s.backends || []).filter(function(b) { return b.state === 'ok'; }).length;
    var total = (s.backends || []).length;
    summary.textContent = total === 0 ? 'no backends' : (ok + ' of ' + total + ' ready');
  }

  // ── Hint line (orange, only shown when something is broken) ────────────
  var hint = document.getElementById('info-hint');
  if (hint) {
    var msg = '';
    if (!s.llm_wired && !s.mcp_wired) msg = 'Settings → Proxies → Connect Claude';
    else if (!s.llm_wired) msg = 'LLM lane off — click Connect Claude';
    else if (!s.mcp_wired) msg = 'MCP lane off — wire voitta in Claude or Codex';
    if (msg) { hint.textContent = msg; hint.setAttribute('style', ''); }
    else hint.setAttribute('style', 'display:none;');
  }

  // ── Headline metric ────────────────────────────────────────────────────
  var savedVal = document.getElementById('info-saved-value');
  if (savedVal) savedVal.textContent = '$' + (s.savings_usd || 0).toFixed(2);
}

function setPill(id, text, kind) {
  var el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.setAttribute('class', 'pill-val ' + kind);
}
function setRectDim(id, dim) {
  var el = document.getElementById(id);
  if (!el) return;
  var cls = 'comp-rect' + (dim ? ' dim' : '');
  el.setAttribute('class', cls);
}
function setIconState(id, live) {
  var el = document.getElementById(id);
  if (!el) return;
  el.setAttribute('class', 'icon-stroke ' + (live ? 'live' : 'dim'));
}
function setMcpIconState(groupId, live) {
  var g = document.getElementById(groupId);
  if (!g) return;
  Array.prototype.forEach.call(g.querySelectorAll('.icon-stroke, [class*=icon-stroke]'), function(node) {
    node.setAttribute('class', 'icon-stroke ' + (live ? 'live' : 'dim'));
  });
}
function setArrowLive(id, live) {
  var el = document.getElementById(id);
  if (!el) return;
  el.setAttribute('class', 'arrow ' + (live ? 'live' : 'dim'));
}
function setArrowTipColor(id, live) {
  var el = document.getElementById(id);
  if (!el) return;
  el.setAttribute('fill', live ? '#ffd83d' : 'rgba(140,180,220,0.16)');
}

// Init from embedded config
renderApps();
loadJira();
loadProxy();
renderToolTree();
// Proxies tab is active by default; reveal the link button and apply
// the live link state injected by Python at popup-open time.
document.getElementById('claude-link-btn').style.display = '';
if (typeof _initialClaudeLinked !== 'undefined') {
  _setClaudeLinkState(_initialClaudeLinked);
}
// Info tab initial render — Python pushes updates every ~3s after this.
if (typeof _initialInfo !== 'undefined') {
  _setInfoState(_initialInfo);
}
