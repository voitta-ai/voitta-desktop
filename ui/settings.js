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
// Subprocess templates: google_mcp/jira_mcp (HTTP, cwd/env_path/port fields),
// http_command (HTTP, arbitrary command + cwd/env_path/port/port_env fields),
// npx (stdio via NpxStdioTransport, package+args fields),
// command (stdio via StdioTransport, command field).

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
    const tplRow = document.createElement('div');
    tplRow.className = 'field';
    const tplLabel = document.createElement('label'); tplLabel.textContent = 'Template';
    const tplSel = document.createElement('select');
    [
      ['google_mcp',   'google_mcp (HTTP subprocess)'],
      ['jira_mcp',     'jira_mcp (HTTP subprocess)'],
      ['http_command', 'http_command (HTTP subprocess — arbitrary command)'],
      ['npx',          'npx (stdio — e.g. chrome-devtools-mcp)'],
      ['command',      'command (stdio — arbitrary executable)'],
    ].forEach(([val, label]) => {
      const opt = document.createElement('option');
      opt.value = val; opt.textContent = label;
      if (sp.template === val) opt.selected = true;
      tplSel.appendChild(opt);
    });
    tplSel.onchange = () => { sp.template = tplSel.value; renderMcpServers(); };
    tplRow.appendChild(tplLabel); tplRow.appendChild(tplSel);
    card.appendChild(tplRow);

    const tpl = sp.template || 'google_mcp';
    if (tpl === 'npx') {
      card.appendChild(_field('Package', _textInputObj(sp, 'package', 'chrome-devtools-mcp@latest')));
      // args stored as space-separated string in UI, array in config
      const argsVal = Array.isArray(sp.args) ? sp.args.join(' ') : (sp.args || '');
      const argsInput = document.createElement('input');
      argsInput.type = 'text';
      argsInput.value = argsVal;
      argsInput.placeholder = '--headless --no-usage-statistics';
      argsInput.onchange = () => {
        sp.args = argsInput.value.trim() ? argsInput.value.trim().split(/\s+/) : [];
      };
      card.appendChild(_field('Args (space-separated)', argsInput));
    } else if (tpl === 'command') {
      const cmdVal = Array.isArray(sp.command) ? sp.command.join(' ') : (sp.command || '');
      const cmdInput = document.createElement('input');
      cmdInput.type = 'text';
      cmdInput.value = cmdVal;
      cmdInput.placeholder = 'node /path/to/server.js';
      cmdInput.onchange = () => {
        sp.command = cmdInput.value.trim() ? cmdInput.value.trim().split(/\s+/) : [];
      };
      card.appendChild(_field('Command (space-separated)', cmdInput));
    } else if (tpl === 'http_command') {
      // Generic HTTP subprocess: user-supplied command that serves MCP over
      // HTTP on `port`. {port}/{env_path} tokens in the command are substituted
      // at launch; port_env (if set) exports the port as that env var.
      const cmdVal = Array.isArray(sp.command) ? sp.command.join(' ') : (sp.command || '');
      const cmdInput = document.createElement('input');
      cmdInput.type = 'text';
      cmdInput.value = cmdVal;
      cmdInput.placeholder = 'python server.py --port {port}';
      cmdInput.onchange = () => {
        sp.command = cmdInput.value.trim() ? cmdInput.value.trim().split(/\s+/) : [];
      };
      card.appendChild(_field('Command (space-separated; {port}, {env_path} tokens)', cmdInput));
      card.appendChild(_field('Working directory', _textInputObj(sp, 'cwd', '~/DEVEL/...')));
      card.appendChild(_field('Env file (optional)', _textInputObj(sp, 'env_path', '~/.voitta_desktop/...env')));
      const portInput = _textInputObj(sp, 'port', '18766');
      portInput.onchange = () => { sp.port = parseInt(portInput.value) || 0; };
      card.appendChild(_field('Port', portInput));
      card.appendChild(_field('Port env var (optional, e.g. PORT)', _textInputObj(sp, 'port_env', 'PORT')));
    } else {
      card.appendChild(_field('Working directory', _textInputObj(sp, 'cwd', '~/DEVEL/...')));
      card.appendChild(_field('Env file', _textInputObj(sp, 'env_path', '~/.voitta_desktop/...env')));
      const portInput = _textInputObj(sp, 'port', '18766');
      portInput.onchange = () => { sp.port = parseInt(portInput.value) || 0; };
      card.appendChild(_field('Port', portInput));
    }
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

  _renderMcpControls(card, s);

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

// ── Subprocess server controls (start/stop/restart + logs) ─────────────────
// Only servers whose process the desktop owns can be controlled: subprocess
// kind with an HTTP-subprocess template. Operations act on the running/saved
// config — unsaved edits need Save + restart to take effect.
const _MCP_CONTROLLABLE_TEMPLATES = ['google_mcp', 'jira_mcp', 'http_command'];
let _mcpNonce = 0;

function _mcpServerId(s) { return (s.prefix || s.name || '').trim(); }

function _b64u(str) { return btoa(unescape(encodeURIComponent(str))); }

// All bridge messages go through a serialized queue: the title-KVO bridge can
// only carry one message at a time (Python reads then resets document.title),
// so two writes in the same tick would drop one. The pump spaces sends out so
// auto-status for N servers + rapid clicks all get delivered.
const _mcpMsgQueue = [];
let _mcpPumpRunning = false;
function _mcpEnqueue(title) {
  _mcpMsgQueue.push(title);
  if (_mcpPumpRunning) return;
  _mcpPumpRunning = true;
  const pump = () => {
    if (!_mcpMsgQueue.length) { _mcpPumpRunning = false; return; }
    document.title = _mcpMsgQueue.shift();
    setTimeout(pump, 120);
  };
  pump();
}

function _mcpCtl(id, action) {
  // nonce after '#' so repeated identical actions still re-fire title KVO;
  // Python strips it before base64-decoding the payload.
  _mcpEnqueue('VOITTA_MCP_CTL:' + _b64u(JSON.stringify({ id: id, action: action })) + '#' + (_mcpNonce++));
}
function _mcpViewLog(id) {
  _mcpEnqueue('VOITTA_MCP_LOG:' + _b64u(id) + '#' + (_mcpNonce++));
}

function _renderMcpControls(card, s) {
  if ((s.kind || 'http') !== 'subprocess') return;
  const tpl = (s.subprocess || {}).template || '';
  if (!_MCP_CONTROLLABLE_TEMPLATES.includes(tpl)) return;
  const id = _mcpServerId(s);
  if (!id) return;

  const row = document.createElement('div');
  row.className = 'field';
  row.style.cssText = 'display:flex; align-items:center; gap:8px; flex-wrap:wrap; border-top:1px solid var(--input-border); padding-top:10px; margin-top:6px;';

  const status = document.createElement('span');
  status.id = 'mcpstatus-' + id;
  status.style.cssText = 'font-size:12px; font-weight:600; margin-right:auto;';
  status.textContent = '● checking…';
  status.style.color = 'var(--text-secondary)';
  row.appendChild(status);

  const mkBtn = (label, fn) => {
    const b = document.createElement('button');
    b.className = 'btn'; b.textContent = label; b.onclick = fn;
    return b;
  };
  row.appendChild(mkBtn('Start',   () => _mcpCtl(id, 'start')));
  row.appendChild(mkBtn('Stop',    () => _mcpCtl(id, 'stop')));
  row.appendChild(mkBtn('Restart', () => _mcpCtl(id, 'restart')));
  row.appendChild(mkBtn('View logs', () => _mcpViewLog(id)));
  card.appendChild(row);

  // Ask Python for the current state once the card is in the DOM.
  setTimeout(() => _mcpCtl(id, 'status'), 0);
}

// Called by Python (evaluateJavaScript) after a control action / status query.
function _setMcpServerStatus(st) {
  if (!st || !st.id) return;
  const el = document.getElementById('mcpstatus-' + st.id);
  if (!el) return;
  const map = {
    running: ['● running' + (st.pid ? ' (pid ' + st.pid + ')' : ''), '#3fb950'],
    stopped: ['● stopped' + (st.code != null ? ' (exit ' + st.code + ')' : ''), 'var(--text-secondary)'],
    crashed: ['● crashed' + (st.code != null ? ' (exit ' + st.code + ')' : ''), '#f85149'],
    unknown: ['● ' + (st.error || 'unknown'), '#f85149'],
  };
  const [text, color] = map[st.state] || map.unknown;
  el.textContent = text;
  el.style.color = color;
}

// Called by Python with the captured stdout/stderr text.
function _setMcpServerLog(payload) {
  if (!payload) return;
  _showMcpLogModal(payload.id || '', payload.text || '');
}

function _showMcpLogModal(id, text) {
  let overlay = document.getElementById('mcp-log-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'mcp-log-overlay';
    overlay.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.5); display:flex; align-items:center; justify-content:center; z-index:1000;';
    overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
    const box = document.createElement('div');
    // resize:both lets the user drag the bottom-right corner to grow/shrink.
    box.style.cssText = 'background:var(--bg, #1e1e1e); border:1px solid var(--input-border); border-radius:10px; width:80%; height:70%; max-width:95vw; max-height:90vh; min-width:360px; min-height:220px; display:flex; flex-direction:column; overflow:hidden; resize:both;';
    const head = document.createElement('div');
    head.style.cssText = 'display:flex; align-items:center; justify-content:space-between; padding:10px 14px; border-bottom:1px solid var(--input-border); flex:0 0 auto;';
    const title = document.createElement('div');
    title.id = 'mcp-log-title';
    title.style.cssText = 'font-weight:600; font-size:13px;';
    const head_btns = document.createElement('div');
    head_btns.style.cssText = 'display:flex; gap:8px;';
    const copy = document.createElement('button');
    copy.className = 'btn'; copy.textContent = 'Copy';
    copy.onclick = () => {
      const t = document.getElementById('mcp-log-body').textContent || '';
      // navigator.clipboard may be unavailable in the webview; fall back to
      // selecting the body so ⌘C works.
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(t).then(
          () => { copy.textContent = 'Copied'; setTimeout(() => copy.textContent = 'Copy', 1200); },
          () => _selectMcpLogBody());
      } else {
        _selectMcpLogBody();
      }
    };
    const refresh = document.createElement('button');
    refresh.className = 'btn'; refresh.textContent = 'Refresh';
    refresh.onclick = () => _mcpViewLog(overlay.dataset.serverId || '');
    const close = document.createElement('button');
    close.className = 'btn'; close.textContent = 'Close';
    close.onclick = () => overlay.remove();
    head_btns.appendChild(copy); head_btns.appendChild(refresh); head_btns.appendChild(close);
    head.appendChild(title); head.appendChild(head_btns);
    const pre = document.createElement('pre');
    pre.id = 'mcp-log-body';
    // user-select:text overrides the global user-select:none (settings.html);
    // cursor:text + the selection styling make it behave like a text area.
    pre.style.cssText = 'margin:0; padding:12px 14px; overflow:auto; font-family:ui-monospace,Menlo,monospace; font-size:11.5px; line-height:1.45; white-space:pre-wrap; word-break:break-word; flex:1 1 auto; -webkit-user-select:text; user-select:text; cursor:text;';
    box.appendChild(head); box.appendChild(pre);
    overlay.appendChild(box);
    document.body.appendChild(overlay);
  }
  overlay.dataset.serverId = id;
  document.getElementById('mcp-log-title').textContent = 'Logs — ' + (id || 'server');
  const body = document.getElementById('mcp-log-body');
  const atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 4;
  body.textContent = text;
  // Only auto-scroll if the user was already at the bottom — don't yank them
  // away from text they're reading/selecting on Refresh.
  if (atBottom) body.scrollTop = body.scrollHeight;
}

function _selectMcpLogBody() {
  const body = document.getElementById('mcp-log-body');
  if (!body) return;
  const range = document.createRange();
  range.selectNodeContents(body);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
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
  document.getElementById('tool_use_ref_min_chars').value =
    (bash.tool_use_ref_min_chars === undefined ? 500 : bash.tool_use_ref_min_chars);

  var time = config.time || {};
  document.getElementById('tool_result_keep_turns').value = time.tool_result_keep_turns || 5;
  document.getElementById('image_keep_turns').value       = time.image_keep_turns       || 5;
  document.getElementById('thinking_keep_turns').value    = time.thinking_keep_turns    || 5;

  var tools = config.tools || {};
  // Default true — Codex signs its handshake, no need to confirm every time.
  document.getElementById('suppress_codex_popup').checked = tools.suppress_codex_popup !== false;
  // Default false — Claude Code windows get the popup unless this is on.
  document.getElementById('suppress_claude_popup').checked = tools.suppress_claude_popup === true;
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
    tool_use_ref_min_chars: Math.max(0, parseInt(document.getElementById('tool_use_ref_min_chars').value) || 0),
  };
  config.time = {
    tool_result_keep_turns: Math.max(1, parseInt(document.getElementById('tool_result_keep_turns').value) || 5),
    image_keep_turns:       Math.max(1, parseInt(document.getElementById('image_keep_turns').value)       || 5),
    thinking_keep_turns:    Math.max(1, parseInt(document.getElementById('thinking_keep_turns').value)    || 5),
  };
  config.tools = {
    suppress_codex_popup: document.getElementById('suppress_codex_popup').checked,
    suppress_claude_popup: document.getElementById('suppress_claude_popup').checked,
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
var _currentTab = 'proxies';

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(function(t) {
    t.classList.toggle('active', t.dataset.tab === name);
  });
  document.querySelectorAll('.tab-pane').forEach(function(p) {
    p.classList.toggle('active', p.id === 'tab-' + name);
  });
  if (name === 'llms') {
    var frame = document.getElementById('llm-frame');
    if (!frame.src) frame.src = _llmUrl;
  }
  // Which mod Claude Code has installed takes `claude plugin list` (slow), so
  // it is asked when the Actions tab opens, not on every refresh.
  if (name === 'actions' && _currentTab !== 'actions') runAction('refresh');
  _currentTab = name;
}

// One title change per message; the salt makes repeats observable.
function _bridge(command) {
  document.title = command + '#' + Math.random().toString(36).slice(2);
}

// The embedded LLM page can't open browser tabs or reach the clipboard from
// inside a WKWebView iframe, so it asks us and we ask the app.
window.addEventListener('message', function(e) {
  if (!/^http:\/\/(localhost|127\.0\.0\.1):\d+$/.test(e.origin)) return;
  var m = e.data && e.data.voitta;
  if (!m) return;
  if (typeof m.open === 'string') _bridge('VOITTA_OPEN_URL:' + encodeURIComponent(m.open));
  if (typeof m.copy === 'string') _bridge('VOITTA_COPY:' + encodeURIComponent(m.copy));
});

if (typeof _initialTab === 'string' && _initialTab) {
  document.addEventListener('DOMContentLoaded', function() { switchTab(_initialTab); });
}

// ── Actions tab ───────────────────────────────────────────────────────────
// State comes from Python (ui/actions.py): _setActionsState(state) with the
// Info refresh and after each action, _setActionResult(name, result) when an
// action finishes. Buttons send "VOITTA_ACTION:<name>" over the title bridge.
// Everything shown is built with textContent: paths and command output are
// not trusted as HTML.

var _actions = null;
var _actionResults = {};

function runAction(name) {
  _bridge('VOITTA_ACTION:' + name);
}

function _setActionsState(s) {
  _actions = s;
  renderActions();
}

function _setActionResult(name, result) {
  _actionResults[name] = result;
  renderActions();
}

function _el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}

// what: an array of strings and {code: "..."} pieces.
function _actRow(state, name, what, change) {
  var row = _el('div', 'act-row');
  var marks = { ok: '✓', todo: '○', bad: '✗', off: '–' };
  row.appendChild(_el('span', 'mark ' + state, marks[state] || '?'));
  row.appendChild(_el('span', 'name', name));
  var cell = _el('span', 'what');
  (what || []).forEach(function(part) {
    if (typeof part === 'string') cell.appendChild(document.createTextNode(part));
    else cell.appendChild(_el('code', null, part.code));
  });
  (change || []).forEach(function(c) { cell.appendChild(_el('span', 'change', c)); });
  row.appendChild(cell);
  return row;
}

function _modAction() {
  var m = (_actions && _actions.mod) || {};
  return (m.installed && m.installed === m.available) ? 'reinstall_mod' : 'install_mod';
}

function _renderResult(box, result) {
  box.textContent = '';
  if (!result) return;
  (result.steps || []).forEach(function(st) {
    box.appendChild(_el('div', 'step ' + (st.ok ? 'ok' : 'bad'), st.step + ': ' + (st.detail || '')));
    var log = st.log || [];
    if (log.length) {
      var d = _el('details');
      d.appendChild(_el('summary', null, log.length + ' command' + (log.length === 1 ? '' : 's') + ' run'));
      d.appendChild(_el('pre', null, log.map(function(c) {
        return '$ ' + c.command + '   (exit ' + c.exit + ')' + (c.output ? '\n' + c.output : '');
      }).join('\n\n')));
      box.appendChild(d);
    }
  });
  if (result.message) box.appendChild(_el('div', 'step ' + (result.ok ? 'ok' : 'bad'), result.message));
}

function renderActions() {
  var s = _actions;
  if (!s) return;
  var busy = s.busy || [];
  var st = s.settings || {}, me = s.mcp_entry || {}, mod = s.mod || null;

  // ── Claude Code ──
  var rows = document.getElementById('act-claude-rows');
  rows.textContent = '';
  var todo = 0;

  if (st.malformed) {
    rows.appendChild(_actRow('bad', 'LLM proxy', [{code: st.path}, ' is not valid JSON; fix it by hand first.']));
    todo++;
  } else if (st.linked) {
    rows.appendChild(_actRow('ok', 'LLM proxy', [{code: st.path}, ' sends Claude Code\'s requests to ', {code: st.llm_url}]));
  } else {
    var ch = (st.changes || []).map(function(c) {
      return c.label + ': ' + (c.old === null ? '(not set)' : c.old) + ' → ' + (c.new === null ? '(removed)' : c.new);
    });
    if (st.upstream_change) {
      ch.push('Voitta upstream: ' + st.upstream_change.old + ' → ' + st.upstream_change.new + ' (your current one, kept behind Voitta)');
    }
    rows.appendChild(_actRow('todo', 'LLM proxy', [{code: st.path}, ' will send Claude Code\'s requests to ', {code: st.llm_url}], ch));
    todo++;
  }

  var url = me.expected_url || '';
  if (!me.present) {
    rows.appendChild(_actRow('todo', 'MCP server', ['will add ', {code: 'voitta'}, ' → ', {code: url},
      ' to Claude Code\'s user MCP servers (', {code: me.config_file || '~/.claude.json'}, ')']));
    todo++;
  } else if (me.header === 'not_http') {
    rows.appendChild(_actRow('off', 'MCP server', [{code: 'voitta'}, ' is set up by hand (not an HTTP entry); left alone']));
  } else if (!me.url_ok) {
    rows.appendChild(_actRow('todo', 'MCP server', [{code: 'voitta'}, ' points at ', {code: me.url || '?'},
      '; will point it at ', {code: url}]));
    todo++;
  } else {
    rows.appendChild(_actRow('ok', 'MCP server', [{code: 'voitta'}, ' → ', {code: url}, ' (user level)']));
  }

  var hdr = me.header;
  if (!me.present || hdr === 'missing' || hdr === 'stale') {
    rows.appendChild(_actRow('todo', 'Window header', ['will add Voitta\'s header script, so ', {code: '/mcp'},
      ' can give one window its own tools']));
    todo++;
  } else if (hdr === 'set') {
    rows.appendChild(_actRow('ok', 'Window header', ['set: ', {code: '/mcp'}, ' can give one window its own tools']));
  } else if (hdr === 'other_helper') {
    rows.appendChild(_actRow('off', 'Window header', ['the entry has a header script of your own; left alone, so ',
      {code: '/mcp'}, ' can\'t give one window its own tools']));
  } else {
    rows.appendChild(_actRow('off', 'Window header', ['n/a']));
  }

  if (!mod) {
    rows.appendChild(_actRow('off', '/llm mod', ['asking Claude Code…']));
  } else if (mod.error) {
    rows.appendChild(_actRow('bad', '/llm mod', ['couldn\'t ask Claude Code: ' + mod.error]));
  } else if (!mod.installed) {
    rows.appendChild(_actRow('todo', '/llm mod', ['will install voitta-llm ' + mod.available]));
    todo++;
  } else if (mod.installed !== mod.available) {
    rows.appendChild(_actRow('todo', '/llm mod', ['will update voitta-llm ' + mod.installed + ' → ' + mod.available]));
    todo++;
  } else {
    rows.appendChild(_actRow('ok', '/llm mod', ['voitta-llm ' + mod.installed + (mod.enabled ? '' : ' (disabled in Claude Code)')]));
  }

  if (!s.claude_found) {
    rows.appendChild(_actRow('bad', 'Claude Code', ['the ', {code: 'claude'}, ' command was not found; install Claude Code first']));
  }

  var configuring = busy.indexOf('configure_claude') >= 0;
  var cfgBtn = document.getElementById('act-configure');
  cfgBtn.disabled = configuring || !s.claude_found;
  cfgBtn.textContent = configuring ? 'Configuring…' : (todo ? 'Configure Claude' : 'Configure Claude again');
  document.getElementById('act-disconnect').style.display = st.linked ? '' : 'none';
  var sum = document.getElementById('act-claude-summary');
  sum.className = 'act-summary' + (todo ? '' : ' ok');
  sum.textContent = configuring ? '' : (todo ? todo + ' thing' + (todo === 1 ? '' : 's') + ' to set up'
                                              : 'Claude Code is set up');
  _renderResult(document.getElementById('act-claude-result'), _actionResults.configure_claude);

  // ── the mod on its own ──
  var mrows = document.getElementById('act-mod-rows');
  mrows.textContent = '';
  var installing = busy.indexOf('install_mod') >= 0 || busy.indexOf('reinstall_mod') >= 0;
  var mbtn = document.getElementById('act-mod');
  if (!mod) {
    mrows.appendChild(_actRow('off', 'voitta-llm', ['asking Claude Code…']));
  } else if (mod.error) {
    mrows.appendChild(_actRow('bad', 'voitta-llm', ['couldn\'t ask Claude Code: ' + mod.error]));
  } else {
    var state = !mod.installed ? 'todo' : (mod.installed === mod.available ? 'ok' : 'todo');
    var line = !mod.installed ? 'not installed (this app has ' + mod.available + ')'
             : (mod.installed === mod.available ? 'installed ' + mod.installed : 'installed ' + mod.installed + '; this app has ' + mod.available);
    mrows.appendChild(_actRow(state, 'voitta-llm', [line + '. In Claude Code: ', {code: '/llm'}, ', ',
      {code: '/voitta-store'}, ', and the tool popup on ', {code: '/mcp'}, '.']));
  }
  var verb = !mod || !mod.installed ? 'Install' : (mod.installed === mod.available ? 'Reinstall' : 'Update');
  mbtn.textContent = !installing ? verb + ' /llm and /voitta-store'
    : (verb === 'Reinstall' ? 'Reinstalling…' : verb === 'Update' ? 'Updating…' : 'Installing…');
  mbtn.disabled = installing || !mod || !!mod.error || !s.claude_found;
  document.getElementById('act-mod-summary').textContent =
    (mod && mod.installed) ? 'Open Claude Code windows pick a new version up after /reload-plugins.' : '';
  _renderResult(document.getElementById('act-mod-result'), _actionResults.install_mod || _actionResults.reinstall_mod);

  // ── tools ──
  var tsum = document.getElementById('act-tools-summary');
  var tr = _actionResults.reset_tool_gate;
  tsum.className = 'act-summary' + (tr && tr.ok ? ' ok' : '');
  tsum.textContent = tr ? tr.message : '';
  var crows = document.getElementById('act-codex-rows');
  crows.textContent = '';
  crows.appendChild(_actRow(s.codex_mcp ? 'ok' : 'off', 'Codex', s.codex_mcp
    ? ['uses Voitta\'s MCP proxy (', {code: '~/.codex/config.toml'}, ')']
    : ['does not use Voitta\'s MCP proxy (', {code: '~/.codex/config.toml'}, '); set up by hand if wanted']));
}

// ── Info tab rendering ────────────────────────────────────────────────────
// Called from Python with a state object every ~3s while the settings
// window is open (ui/settings_window.py _collect_info_state). The whole
// diagram is drawn from that state each time, so the number of accounts and
// MCP servers shown follows the data. Labels come from config and are
// escaped. Coordinates are in the 920×600 viewBox.
//
//   CLAUDE CODE (setup chips)             CODEX (config chip)
//        │ LLM lane          │ MCP lane          │
//   LLM PROXY ───────────────┼──────────── MCP PROXY (health)
//   OPTIMIZER                               TOOL GATE
//   ROUTER                                  │
//   [account] [account] …                  [server] [server] …
//   ACTIVE · STORED · SAVED

function _svgEsc(s) {
  return String(s === undefined || s === null ? '' : s).replace(/[&<>"']/g, function(c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

function _cut(s, n) {
  s = String(s || '');
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

function _box(x, y, w, h, opts) {
  opts = opts || {};
  var cls = 'comp-rect' + (opts.dim ? ' dim' : '') + (opts.cls ? ' ' + opts.cls : '');
  var g = '<g transform="translate(' + x + ',' + y + ')"' +
    (opts.onclick ? ' class="clickable" onclick="' + opts.onclick + '"' : '') + '>';
  if (opts.title) g += '<title>' + _svgEsc(opts.title) + '</title>';
  g += '<rect class="' + cls + '" width="' + w + '" height="' + h + '" rx="' + (opts.rx || 10) + '"/>';
  if (opts.label) g += '<text class="comp-label" x="' + (w / 2) + '" y="' + (opts.labelY || 18) + '">' +
    _svgEsc(opts.label) + '</text>';
  (opts.lines || []).forEach(function(line, i) {
    g += '<text class="comp-sub' + (line.cls ? ' ' + line.cls : '') + '" x="' + (w / 2) + '" y="' +
      ((opts.labelY || 18) + 15 + i * 13) + '">' + _svgEsc(line.text !== undefined ? line.text : line) + '</text>';
  });
  return g + (opts.inner || '') + '</g>';
}

function _arrow(d, live, tip) {
  var s = '<path class="arrow ' + (live ? 'live' : 'dim') + '" d="' + d + '"/>';
  if (tip) {
    var x = tip[0], y = tip[1];
    s += '<polygon points="' + (x - 5) + ',' + (y - 8) + ' ' + (x + 5) + ',' + (y - 8) + ' ' + x + ',' + (y + 2) +
      '" fill="' + (live ? '#ffd83d' : 'rgba(140,180,220,0.16)') + '"/>';
  }
  return s;
}

// A small status chip: ✓ ok, ○ to do, ✗ problem, – off.
function _chip(x, y, w, text, state, title, onclick) {
  var mark = { ok: '✓', todo: '○', bad: '✗', off: '–' }[state] || '?';
  return '<g transform="translate(' + x + ',' + y + ')" class="clickable" onclick="' + (onclick || "switchTab('actions')") + '">' +
    '<title>' + _svgEsc(title) + '</title>' +
    '<rect class="chip ' + state + '" width="' + w + '" height="20" rx="10"/>' +
    '<text class="chip-text ' + state + '" x="' + (w / 2) + '" y="14">' + mark + ' ' + _svgEsc(text) + '</text></g>';
}

function _pills(x, y, w, rows, onclick) {
  var s = '<g transform="translate(' + x + ',' + y + ')"' + (onclick ? ' class="clickable" onclick="' + onclick + '"' : '') + '>';
  s += '<rect class="pill-bg" width="' + w + '" height="' + (rows.length * 18 + 8) + '" rx="6"/>';
  rows.forEach(function(r, i) {
    s += '<text class="pill-key" x="10" y="' + (18 + i * 18) + '">' + _svgEsc(r[0]) + '</text>' +
      '<text class="pill-val ' + (r[2] || 'neutral') + '" x="' + (w - 10) + '" y="' + (18 + i * 18) + '">' +
      _svgEsc(r[1]) + '</text>';
  });
  return s + '</g>';
}

// Up to five boxes in a row; past that, four and a "+N more" naming the rest.
function _fitRow(items, name) {
  if (items.length <= 5) return items;
  var rest = items.slice(4);
  return items.slice(0, 4).concat([{ more: rest.length, names: rest.map(name) }]);
}

// How many characters of text fit a box of width w (charW: px per character).
function _fitChars(w, charW) {
  return Math.max(4, Math.floor((w - 10) / charW));
}

// A row of boxes under (cx, top): a bus from the parent, a drop to each box.
function _fan(items, x0, x1, top, live, render) {
  var n = items.length;
  if (!n) return '';
  var gap = 10, w = Math.min(130, Math.floor((x1 - x0 - gap * (n - 1)) / n));
  var total = n * w + gap * (n - 1), start = x0 + Math.floor((x1 - x0 - total) / 2);
  var centers = items.map(function(_, i) { return start + i * (w + gap) + w / 2; });
  var s = '';
  if (n > 1) s += _arrow('M ' + centers[0] + ' ' + (top - 14) + ' H ' + centers[n - 1], live);
  centers.forEach(function(c, i) {
    s += _arrow('M ' + c + ' ' + (top - 14) + ' V ' + (top - 2), live, [c, top - 2]);
    s += render(items[i], start + i * (w + gap), top, w);
  });
  return { svg: s, centers: centers };
}

function _setInfoState(s) {
  if (!s) return;
  var svg = document.getElementById('info-svg');
  if (!svg) return;
  var llm = s.llm || {}, setup = s.setup || {}, gate = s.gate || {}, health = s.mcp_health || {};
  var accts = llm.accounts || [], backends = s.backends || [];
  var def = accts.filter(function(a) { return a.is_default; })[0];
  var llmLive = !!s.llm_wired;
  var claudeMcp = !!s.mcp_wired_claude, codexMcp = !!s.mcp_wired_codex;
  var mcpBroken = health.state === 'broken';
  var mcpLive = (claudeMcp || codexMcp) && !mcpBroken;
  var out = '';

  // ── top: routing and MCP server health ──
  var last = llm.last_answer;
  out += _pills(10, 8, 230, [
    ['DEFAULT LLM', def ? _cut(def.short, 16).toUpperCase() : '—', def && def.state === 'error' ? 'off' : 'neutral'],
    ['LAST ANSWERED BY', _cut(last ? last.model : (s.current_model || '—'), 18).toUpperCase(), 'neutral'],
  ], "switchTab('llms')");
  out += _pills(680, 8, 230, [
    ['MCP SERVER', mcpBroken ? '✗ FAILING' : (health.state === 'healthy' ? '✓ HEALTHY' : '…'), mcpBroken ? 'off' : 'ok'],
    ['TOOLS ON', (gate.tools ? (gate.tools - (gate.disabled || 0)) + ' / ' + gate.tools : '—'), 'neutral'],
  ], "switchTab('tools')");

  // ── clients ──
  var mod = setup.mod || {};
  var modState = !setup.mod_known ? 'off' : (!mod.installed ? 'todo' : (mod.installed === mod.available ? 'ok' : 'todo'));
  var hdr = setup.header;
  var chips =
    _chip(18, 54, 104, 'LLM proxy', setup.settings_linked ? 'ok' : 'todo',
          '~/.claude/settings.json → ANTHROPIC_BASE_URL' + (setup.settings_linked ? ' points at Voitta' : ': not set to Voitta')) +
    _chip(128, 54, 104, 'MCP entry', setup.mcp_entry_ok ? 'ok' : 'todo',
          'Claude Code user MCP server "voitta"' + (setup.mcp_entry_ok ? ' → Voitta' : ': missing or elsewhere')) +
    _chip(238, 54, 104, 'win header', hdr === 'set' ? 'ok' : (hdr === 'other_helper' || hdr === 'not_http' ? 'off' : 'todo'),
          'Header script that lets /mcp give one window its own tools: ' + (hdr || 'n/a')) +
    _chip(348, 54, 104, '/llm mod', modState,
          'voitta-llm in Claude Code: ' + (!setup.mod_known ? 'not checked yet (open Actions)' :
          (mod.installed ? mod.installed + (mod.installed === mod.available ? '' : ', app has ' + mod.available) : 'not installed')));
  var n = s.active_conversations || 0, picked = llm.picked_windows || 0;
  out += _box(150, 74, 470, 84, {
    label: 'CLAUDE CODE', title: 'Claude Code: what is set up for your user (click: Settings → Actions)',
    onclick: "switchTab('actions')",
    lines: [(n === 1 ? '1 conversation' : n + ' conversations') + (picked ? ' · ' + picked + ' on their own /llm account' : '')],
    inner: chips,
  });
  out += _box(660, 74, 180, 84, {
    label: 'CODEX', dim: !codexMcp, title: 'Codex reaches MCP tools through Voitta when ~/.codex/config.toml points at it',
    lines: ['MCP only'],
    inner: _chip(38, 54, 104, 'config.toml', codexMcp ? 'ok' : 'off', '~/.codex/config.toml → Voitta MCP proxy: ' + (codexMcp ? 'yes' : 'no'), "switchTab('actions')"),
  });

  // ── lanes from the clients to the proxies ──
  out += _arrow('M 250 158 V 206', llmLive, [250, 206]);
  out += _arrow('M 560 158 V 182 H 650 V 206', claudeMcp && !mcpBroken, [650, 206]);
  out += _arrow('M 750 158 V 206', codexMcp && !mcpBroken, [750, 206]);

  // ── proxies ──
  out += _box(110, 208, 280, 58, {
    label: 'LLM PROXY', dim: !llmLive, onclick: "switchTab('proxies')", title: 'Settings → Proxies',
    lines: [':' + ((s.llm_proxy || {}).port || '?') + (llmLive ? '' : ' · Claude Code not pointed here')],
  });
  out += _box(560, 208, 280, 58, {
    label: 'MCP PROXY', dim: !(claudeMcp || codexMcp), cls: mcpBroken ? 'broken' : '',
    onclick: "switchTab('proxies')", title: 'Settings → Proxies',
    lines: [{ text: ':' + ((s.mcp_proxy || {}).port || '?') + (mcpBroken ? ' · failing: restart Voitta Desktop' : ''),
              cls: mcpBroken ? 'warn' : '' }],
  });

  // ── LLM side: optimizer → router → accounts ──
  var opt = s.optimizer || {};
  out += _arrow('M 250 266 V 286', llmLive, [250, 286]);
  out += _box(150, 288, 200, 44, {
    label: 'OPTIMIZER', dim: !opt.enabled, onclick: "switchTab('proxies')", labelY: 17,
    title: 'Context optimizers (Settings → Proxies)',
    lines: [opt.enabled ? ('on' + (opt.haiku_only ? ' · haiku only' : '') + ' · saved $' + (s.savings_usd || 0).toFixed(2)) : 'off'],
  });
  out += _arrow('M 250 332 V 352', llmLive, [250, 352]);
  out += _box(150, 354, 200, 44, {
    label: 'ROUTER', onclick: "switchTab('llms')", labelY: 17, dim: !llmLive,
    title: 'Each Claude Code window goes to its /llm pick, or to the default (Settings → LLMs)',
    lines: ['default: ' + (def ? _cut(def.short, 14) : '—') + (picked ? ' · ' + picked + ' on /llm' : '')],
  });
  var shownA = _fitRow(accts, function(a) { return a.label; });
  var fanA = _fan(shownA, 10, 450, 432, llmLive, function(a, x, y, w) {
    if (a.more) return _box(x, y, w, 64, { label: '+' + a.more + ' MORE', onclick: "switchTab('llms')", labelY: 36,
                                           title: a.names.join('\n') });
    var st = { ok: 'ok', empty: 'todo', error: 'bad' }[a.state] || 'off';
    return _box(x, y, w, 64, {
      label: _cut(a.short, _fitChars(w, 8)).toUpperCase(), labelY: 17, cls: a.is_default ? 'default' : '',
      onclick: "switchTab('llms')",
      title: a.label + ' (' + a.provider + ')' + (a.is_default ? ' · default' : '') +
        (a.windows ? ' · ' + a.windows + (a.windows === 1 ? ' window' : ' windows') + ' picked it' : '') +
        (a.status !== 'ok' ? ' · ' + a.status.replace('_', ' ') : ''),
      lines: [_cut(a.label, _fitChars(w, 5.6)), { text: (a.is_default ? 'default' : (a.windows ? a.windows + ' on /llm' : ' ')) +
        (st === 'ok' ? '' : ' · ' + a.status.replace('_', ' ')), cls: st === 'ok' ? '' : 'warn' }],
    });
  });
  if (fanA) out += _arrow('M 250 398 V 418', llmLive) + fanA.svg;

  // ── MCP side: tool gate → servers ──
  var gateLine = 'Claude ' + (gate.claude_silent ? 'silent' : 'popup') + ' · Codex ' + (gate.codex_silent ? 'silent' : 'popup');
  out += _arrow('M 700 266 V 286', mcpLive, [700, 286]);
  out += _box(600, 288, 200, 58, {
    label: 'TOOL GATE', labelY: 17, onclick: "switchTab('tools')", dim: !mcpLive,
    title: 'Which tools each client gets (Settings → Tools); /mcp gives one Claude Code window its own',
    lines: [gateLine, (gate.disabled || 0) + ' tool' + (gate.disabled === 1 ? '' : 's') + ' switched off'],
  });
  var shownB = _fitRow(backends, function(b) { return b.label; });
  var fanB = _fan(shownB, 470, 910, 432, mcpLive, function(b, x, y, w) {
    if (b.more) return _box(x, y, w, 64, { label: '+' + b.more + ' MORE', onclick: "switchTab('mcps')", labelY: 36,
                                           title: b.names.join('\n') });
    var st = b.state === 'ok' ? 'ok' : (b.state === 'error' ? 'bad' : 'todo');
    return _box(x, y, w, 64, {
      label: _cut(b.label, _fitChars(w, 8)).toUpperCase(), labelY: 17, onclick: "switchTab('mcps')",
      dim: b.state !== 'ok',
      title: b.label + ' — ' + (b.state === 'ok' ? b.tools_count + ' tools' : b.state),
      lines: [{ text: b.state === 'ok' ? b.tools_count + ' tools' : (b.state === 'error' ? 'not answering' : 'no tools yet'),
                cls: st === 'ok' ? '' : 'warn' }],
    });
  });
  if (fanB) out += _arrow('M 700 346 V 418', mcpLive) + fanB.svg;
  if (!backends.length) out += '<text class="comp-sub" x="700" y="460">no MCP servers (Settings → MCPs)</text>';

  // ── footer: hint, conversations, savings ──
  var msg = '';
  if (mcpBroken) msg = 'MCP server failing since a while — restart Voitta Desktop';
  else if (def && def.state === 'error') msg = 'Default LLM account needs attention — Settings → LLMs';
  else if (!llmLive || !claudeMcp || hdr === 'missing' || hdr === 'stale' || modState === 'todo')
    msg = 'Claude Code is not fully set up — Settings → Actions → Configure Claude';
  if (msg) out += '<text class="hint-text" x="460" y="524">' + _svgEsc(msg) + '</text>';
  out += _pills(10, 540, 230, [['ACTIVE CONVERSATIONS', String(n), 'neutral']]);
  out += _pills(250, 540, 230, [['STORED CONVERSATIONS', String(s.stored_conversations || 0), 'neutral']],
                "runAction('stored_conversations')");
  out += '<g transform="translate(500,536)"><rect class="saved-bg" width="410" height="40" rx="8"/>' +
    '<text class="saved-label" x="20" y="26">SAVED</text>' +
    '<text class="saved-value" x="396" y="29">$' + (s.savings_usd || 0).toFixed(2) + '</text></g>';

  svg.innerHTML = out;
}

// Init from embedded config
renderApps();
renderMcpServers();
loadJira();
loadProxy();
renderToolTree();
// Actions tab: first render from the state injected at popup open; Python
// pushes updates with the Info refresh and after each action.
if (typeof _initialActions !== 'undefined' && _initialActions) {
  _setActionsState(_initialActions);
}
// Info tab initial render — Python pushes updates every ~3s after this.
if (typeof _initialInfo !== 'undefined') {
  _setInfoState(_initialInfo);
}
