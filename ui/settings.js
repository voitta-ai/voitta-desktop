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
          <button class="btn btn-danger" onclick="deleteApp(${i})">${_pendingAppDeletes.has(i) ? 'Click again to confirm' : 'Delete'}</button>
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

// Two-stage delete pattern — WKWebView blocks prompt()/confirm()/alert() by
// default, so the previous "type the name to confirm" flow silently returned
// null and the delete never ran. First click flips the button to a confirm
// state; second click within 3s actually deletes. Used by both Accounts and
// MCPs tabs.
const _pendingAppDeletes = new Set();
const _pendingMcpDeletes = new Set();

function _armDelete(set, key, rerender) {
  if (set.has(key)) {
    set.delete(key);
    return true;  // caller should perform delete
  }
  set.add(key);
  rerender();
  setTimeout(() => {
    if (set.delete(key)) rerender();
  }, 3000);
  return false;
}

function deleteApp(i) {
  if (_armDelete(_pendingAppDeletes, i, renderApps)) {
    config.apps.splice(i, 1);
    renderApps();
  }
}

// ── MCP servers ─────────────────────────────────────────────────────────────
// Each server is a card with: name, prefix, description, kind (http/subprocess),
// transport-specific fields, and auth selector with conditional auth fields.
// The subprocess kind exposes a `template` (google_mcp / jira_mcp) — the command
// shape is fixed by template; only cwd/env_path/port are user-editable.

const AUTH_LABELS = {
  none: 'None / open',
  bearer: 'Bearer token',
  api_key: 'API key header',
  basic: 'Basic auth (user:pass)',
  custom_headers: 'Custom headers',
  oauth_app: 'OAuth (linked Auth app)',
  voitta_rag_legacy: 'Voitta RAG (legacy)',
};

function _mcpAuth(s) { return s.auth || (s.auth = { type: 'none' }); }

function renderMcpServers() {
  const c = document.getElementById('mcps-container');
  if (!c) return;
  c.innerHTML = '';
  const servers = config.mcp_servers || (config.mcp_servers = []);
  servers.forEach((s, i) => c.appendChild(_renderMcpCard(s, i)));
}

function _renderMcpCard(s, i) {
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.index = i;

  const header = document.createElement('div');
  header.className = 'card-header';
  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.value = s.name || '';
  nameInput.placeholder = 'Display name';
  nameInput.onchange = () => { s.name = nameInput.value; };
  header.appendChild(nameInput);
  card.appendChild(header);

  card.appendChild(_field('Prefix (tool namespace)', _textInput(s, 'prefix', 'e.g. vim, voitta_rag')));
  card.appendChild(_field('Description (sent to LLM)', _textarea(s, 'description', 'Short summary the model sees')));

  // Kind toggle
  const kindRow = document.createElement('div');
  kindRow.className = 'field';
  const kindLabel = document.createElement('label'); kindLabel.textContent = 'Kind';
  const kindSel = document.createElement('select');
  ['http', 'subprocess'].forEach(k => {
    const opt = document.createElement('option');
    opt.value = k; opt.textContent = k === 'http' ? 'HTTP' : 'Subprocess';
    if ((s.kind || 'http') === k) opt.selected = true;
    kindSel.appendChild(opt);
  });
  kindSel.onchange = () => { s.kind = kindSel.value; renderMcpServers(); };
  kindRow.appendChild(kindLabel); kindRow.appendChild(kindSel);
  card.appendChild(kindRow);

  if ((s.kind || 'http') === 'http') {
    card.appendChild(_field('URL', _textInput(s, 'url', 'https://...')));
  } else {
    const sp = s.subprocess || (s.subprocess = {});
    // Template (read-only in v1 — google_mcp / jira_mcp / custom)
    const tplRow = document.createElement('div');
    tplRow.className = 'field';
    const tplLabel = document.createElement('label'); tplLabel.textContent = 'Template';
    const tplSel = document.createElement('select');
    ['google_mcp', 'jira_mcp'].forEach(t => {
      const opt = document.createElement('option');
      opt.value = t; opt.textContent = t;
      if (sp.template === t) opt.selected = true;
      tplSel.appendChild(opt);
    });
    tplSel.onchange = () => { sp.template = tplSel.value; };
    tplRow.appendChild(tplLabel); tplRow.appendChild(tplSel);
    card.appendChild(tplRow);

    card.appendChild(_field('Working directory', _textInputObj(sp, 'cwd', '~/DEVEL/...')));
    card.appendChild(_field('Env file', _textInputObj(sp, 'env_path', '~/.voitta_desktop/...env')));
    const portInput = _textInputObj(sp, 'port', '18766');
    portInput.onchange = () => { sp.port = parseInt(portInput.value) || 0; };
    card.appendChild(_field('Port', portInput));
  }

  // Auth section
  const authRow = document.createElement('div');
  authRow.className = 'field';
  const authLabel = document.createElement('label'); authLabel.textContent = 'Auth';
  const authSel = document.createElement('select');
  Object.keys(AUTH_LABELS).forEach(t => {
    const opt = document.createElement('option');
    opt.value = t; opt.textContent = AUTH_LABELS[t];
    if (_mcpAuth(s).type === t) opt.selected = true;
    authSel.appendChild(opt);
  });
  authSel.onchange = () => {
    s.auth = { type: authSel.value };
    renderMcpServers();
  };
  authRow.appendChild(authLabel); authRow.appendChild(authSel);
  card.appendChild(authRow);

  _renderAuthFields(card, s);

  // Delete (two-stage: first click arms, second click within 3s deletes)
  const footer = document.createElement('div');
  footer.className = 'card-footer';
  const del = document.createElement('button');
  del.className = 'btn btn-danger';
  del.textContent = _pendingMcpDeletes.has(i) ? 'Click again to confirm' : 'Delete';
  del.onclick = () => deleteMcpServer(i);
  footer.appendChild(del);
  card.appendChild(footer);

  return card;
}

function _renderAuthFields(card, s) {
  const a = _mcpAuth(s);
  if (a.type === 'bearer') {
    card.appendChild(_field('Token', _passwordObj(a, 'token', 'paste token')));
  } else if (a.type === 'api_key') {
    if (!a.header) a.header = 'X-API-Key';
    card.appendChild(_field('Header name', _textInputObj(a, 'header', 'X-API-Key')));
    card.appendChild(_field('Header value', _passwordObj(a, 'value', 'paste key')));
  } else if (a.type === 'basic') {
    card.appendChild(_field('Username', _textInputObj(a, 'username', '')));
    card.appendChild(_field('Password', _passwordObj(a, 'password', '')));
  } else if (a.type === 'custom_headers') {
    if (!Array.isArray(a.headers)) a.headers = [];
    a.headers.forEach((h, hi) => {
      const row = document.createElement('div');
      row.className = 'field';
      row.style.display = 'grid';
      row.style.gridTemplateColumns = '1fr 1fr auto';
      row.style.gap = '6px';
      const n = document.createElement('input');
      n.type = 'text'; n.placeholder = 'Header name'; n.value = h.name || '';
      n.onchange = () => { h.name = n.value; };
      const v = document.createElement('input');
      v.type = 'text'; v.placeholder = 'Header value'; v.value = h.value || '';
      v.onchange = () => { h.value = v.value; };
      const rm = document.createElement('button');
      rm.className = 'btn'; rm.textContent = '−';
      rm.onclick = () => { a.headers.splice(hi, 1); renderMcpServers(); };
      row.appendChild(n); row.appendChild(v); row.appendChild(rm);
      card.appendChild(row);
    });
    const addBtn = document.createElement('button');
    addBtn.className = 'btn'; addBtn.textContent = '+ Add header';
    addBtn.onclick = () => { a.headers.push({ name: '', value: '' }); renderMcpServers(); };
    card.appendChild(addBtn);
  } else if (a.type === 'oauth_app') {
    if (!a.backend) a.backend = 'google_workspace';
    if (!a.app_type) a.app_type = 'google';
    card.appendChild(_field('Backend (apps tab)', _textInputObj(a, 'backend', 'google_workspace')));
    card.appendChild(_field('App type', _textInputObj(a, 'app_type', 'google or microsoft')));
  } else if (a.type === 'voitta_rag_legacy') {
    const note = document.createElement('div');
    note.style.cssText = 'font-size:11px; color:var(--text-secondary); padding:4px 0;';
    note.textContent = 'Legacy multi-app X-Auth-Token-{Microsoft,Google} scheme. Uses all rag-enabled apps from the Accounts tab.';
    card.appendChild(note);
  }
  // type === 'none': no extra fields
}

function _field(labelText, control) {
  const row = document.createElement('div');
  row.className = 'field';
  const lbl = document.createElement('label'); lbl.textContent = labelText;
  row.appendChild(lbl);
  row.appendChild(control);
  return row;
}
function _textInput(obj, key, ph) {
  const i = document.createElement('input');
  i.type = 'text'; i.placeholder = ph || ''; i.value = obj[key] || '';
  i.onchange = () => { obj[key] = i.value; };
  return i;
}
function _textInputObj(obj, key, ph) { return _textInput(obj, key, ph); }
function _passwordObj(obj, key, ph) {
  const i = document.createElement('input');
  i.type = 'password'; i.placeholder = ph || ''; i.value = obj[key] || '';
  i.onchange = () => { obj[key] = i.value; };
  return i;
}
function _textarea(obj, key, ph) {
  const t = document.createElement('textarea');
  t.placeholder = ph || ''; t.value = obj[key] || '';
  t.style.cssText = 'width:100%; min-height:48px; font-family:inherit; padding:6px; border-radius:6px; border:1px solid var(--input-border); background:var(--input-bg); color:var(--text); resize:vertical;';
  t.onchange = () => { obj[key] = t.value; };
  return t;
}

function addMcpServer() {
  if (!Array.isArray(config.mcp_servers)) config.mcp_servers = [];
  config.mcp_servers.push({
    id: uuid(),
    name: 'New MCP Server',
    prefix: 'my_mcp',
    description: '',
    kind: 'http',
    url: '',
    auth: { type: 'none' },
  });
  renderMcpServers();
}

function deleteMcpServer(i) {
  if (_armDelete(_pendingMcpDeletes, i, renderMcpServers)) {
    config.mcp_servers.splice(i, 1);
    renderMcpServers();
  }
}

function collectMcpServers() {
  // The card inputs mutate config.mcp_servers entries in place via onchange
  // handlers, so by the time Save fires this list is already up to date.
  // Just return it. We do strip any entry with an empty prefix on the way
  // out — those would be skipped by the proxy anyway and would clutter the
  // help text.
  return (config.mcp_servers || []).filter(s => (s.prefix || '').trim() !== '');
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
  document.getElementById('proxy_port').value = mcp.port || '18765';
  document.getElementById('llm_proxy_port').value = llm.port || '18900';
  document.getElementById('llm_upstream_url').value = llm.upstream_url || 'https://api.anthropic.com';
  document.getElementById('oauth_redirect_port').value = oauth.redirect_port || '53214';

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
  // mcp_proxy: only the listener port now. Per-backend URLs live in
  // config.mcp_servers. Preserve anything else the saved file has so a
  // rollback to an older Voitta Desktop build keeps reading what it expects.
  config.mcp_proxy = Object.assign({}, config.mcp_proxy || {}, {
    port: parseInt(document.getElementById('proxy_port').value) || 18765,
  });
  config.llm_proxy = {
    port: parseInt(document.getElementById('llm_proxy_port').value) || 18900,
    upstream_url: document.getElementById('llm_upstream_url').value.trim() || 'https://api.anthropic.com',
  };
  config.oauth = {
    redirect_port: parseInt(document.getElementById('oauth_redirect_port').value) || 53214,
  };
  // mcp_subprocess legacy block — preserved for rollback, no longer edited
  // from the Proxies tab. Subprocess parameters now live inside
  // config.mcp_servers[*].subprocess.
  config.mcp_servers = collectMcpServers();
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
renderMcpServers();
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
