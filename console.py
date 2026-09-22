"""console.py - 网页控制台。

为什么是网页而不是桌面窗口：这个项目是纯 Python + 零依赖的，而运行它的
Python 是精简版，**没有 tkinter**。要在不引入打包大件的前提下给一个能点的
界面，最省事的就是复用 bridge.py 已经开着的那个 HTTP 口——
浏览器就是现成的 GUI，而且拿手机也能开。

页面上能做的四件事：
  · 开关打电话（Agent 那一路 / Codex 那一路，各自一个勾）
  · 换音色（Windows 自带 / Edge 在线，两套引擎一起列）
  · 调语速、音调、音量
  · 改 Agent 干完活要念的那句口令

"试听"合成的是**和话机听到的一模一样的 8 kHz 音频** —— 不是高保真预览。
目的就是让"你听到的"等于"话机听到的"，别试听很好、打过去变了个声音。

这个文件只有一块 HTML 字符串，页面里的数据全靠 /api/* 接口拿，
所以改外观不用碰 Python 逻辑。
"""

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vibe Phone 控制台</title>
<style>
  :root{
    --bg:#12161c; --panel:#1a2029; --panel2:#212936; --line:#2c3644;
    --fg:#e8edf4; --dim:#8d9aab; --accent:#4f9bff; --accent2:#2f7ae5;
    --ok:#3ecf8e; --warn:#f0b429; --err:#ff6b6b;
    --radius:10px;
  }
  *{box-sizing:border-box}
  body{
    margin:0; padding:24px 16px 48px;
    background:var(--bg); color:var(--fg);
    font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
  }
  .wrap{max-width:720px;margin:0 auto}
  h1{font-size:20px;margin:0 0 4px;letter-spacing:.3px}
  .sub{color:var(--dim);font-size:13px;margin-bottom:22px}
  .card{
    background:var(--panel);border:1px solid var(--line);
    border-radius:var(--radius);padding:18px 18px 16px;margin-bottom:16px;
  }
  .card h2{
    font-size:13px;font-weight:600;color:var(--dim);
    margin:0 0 14px;letter-spacing:1.2px;text-transform:uppercase;
  }
  label.row{display:block;margin-bottom:14px}
  label.row:last-child{margin-bottom:0}
  .lbl{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
  .lbl span:first-child{font-size:13px;color:var(--dim)}
  .val{font-variant-numeric:tabular-nums;font-size:13px;color:var(--accent)}
  select,textarea,input[type=text]{
    width:100%;background:var(--panel2);color:var(--fg);
    border:1px solid var(--line);border-radius:8px;
    padding:9px 11px;font:inherit;font-size:14px;outline:none;
  }
  select:focus,textarea:focus{border-color:var(--accent)}
  textarea{resize:vertical;min-height:78px;line-height:1.7}
  input[type=range]{
    -webkit-appearance:none;appearance:none;width:100%;height:4px;
    background:var(--line);border-radius:2px;outline:none;
  }
  input[type=range]::-webkit-slider-thumb{
    -webkit-appearance:none;width:16px;height:16px;border-radius:50%;
    background:var(--accent);cursor:pointer;border:2px solid var(--panel);
  }
  .seg{display:flex;gap:8px}
  .seg button{
    flex:1;background:var(--panel2);color:var(--dim);
    border:1px solid var(--line);border-radius:8px;
    padding:9px 12px;font:inherit;font-size:14px;cursor:pointer;
  }
  .seg button.on{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  .seg button small{display:block;font-size:11px;opacity:.75;font-weight:400}
  label.sw{
    display:flex;align-items:flex-start;gap:11px;
    background:var(--panel2);border:1px solid var(--line);border-radius:8px;
    padding:11px 13px;margin-bottom:10px;cursor:pointer;
  }
  label.sw:last-of-type{margin-bottom:0}
  label.sw.off{opacity:.55}
  label.sw input{
    flex:0 0 auto;width:17px;height:17px;margin:3px 0 0;
    accent-color:var(--accent);cursor:pointer;
  }
  .sw-text{font-size:14px;line-height:1.45}
  .sw-text em{display:block;font-style:normal;font-size:12px;color:var(--dim);margin-top:3px}
  .sw-tag{font-size:11px;font-weight:600;margin-left:6px}
  .sw-tag.on{color:var(--ok)} .sw-tag.off{color:var(--warn)}
  .hint{font-size:12px;color:var(--dim);margin-top:8px}
  .hint.err{color:var(--err)}
  .hint.warn{color:var(--warn)}
  .actions{display:flex;gap:10px;flex-wrap:wrap}
  .actions button{
    flex:1;min-width:120px;border-radius:8px;padding:11px 16px;
    font:inherit;font-size:14px;font-weight:600;cursor:pointer;border:1px solid transparent;
  }
  .primary{background:var(--accent);color:#fff}
  .primary:hover{background:var(--accent2)}
  .ghost{background:transparent;color:var(--fg);border-color:var(--line)}
  .ghost:hover{border-color:var(--accent)}
  button:disabled{opacity:.5;cursor:not-allowed}
  #status{margin-top:14px;font-size:13px;min-height:20px;color:var(--dim)}
  #status.ok{color:var(--ok)} #status.err{color:var(--err)}
  audio{width:100%;margin-top:12px;height:36px}
  .dirty{color:var(--warn)}
  code{background:var(--panel2);padding:1px 5px;border-radius:4px;font-size:12px}
</style>
</head>
<body>
<div class="wrap">
  <h1>Vibe Phone 控制台</h1>
  <div class="sub">改完点"保存"，以后 Agent 打电话都按这套设置念。</div>

  <div class="card">
    <h2>打电话开关</h2>
    <label class="sw" id="swAgentRow">
      <input type="checkbox" id="swAgent">
      <span class="sw-text">
        Agent 干完活汇报<span class="sw-tag" id="swAgentTag"></span>
        <em><code>say.py</code> · WorkBuddy 干完活时打的那些电话</em>
      </span>
    </label>
    <label class="sw" id="swCodexRow">
      <input type="checkbox" id="swCodex">
      <span class="sw-text">
        Codex 干完一轮汇报<span class="sw-tag" id="swCodexTag"></span>
        <em>Codex 每轮结束由它自己触发的电话（长度不够的闲聊本来就不打）</em>
      </span>
    </label>
    <div class="hint">
      不勾＝电话根本不会响，从闸门上就拦住了。<b>点一下就生效，不用点"保存"</b>。
      <br>"立即拨一通"按钮<b>不受这两个开关限制</b>，随时能手动验证链路。
    </div>
  </div>

  <div class="card">
    <h2>引擎</h2>
    <div class="seg" id="segBackend">
      <button data-backend="sapi" type="button">Windows 自带<small>离线 · 中文通常只有 1 个嗓子</small></button>
      <button data-backend="edge" type="button">Edge 在线<small>联网 · 中文十几个，有男声</small></button>
    </div>
    <div class="hint" id="backendHint"></div>
  </div>

  <div class="card">
    <h2>音色</h2>
    <label class="row">
      <div class="lbl"><span>选一个嗓子</span><span class="val" id="voiceCount"></span></div>
      <select id="voice"></select>
    </label>
    <label class="row">
      <div class="lbl"><span>语言范围</span></div>
      <select id="localeFilter">
        <option value="zh">中文</option>
        <option value="all">全部语言</option>
      </select>
    </label>
  </div>

  <div class="card">
    <h2>声调</h2>
    <label class="row">
      <div class="lbl"><span>语速</span><span class="val" id="rateVal">+0%</span></div>
      <input type="range" id="rate" min="-50" max="50" step="5" value="0">
    </label>
    <label class="row" id="pitchRow">
      <div class="lbl"><span>音调</span><span class="val" id="pitchVal">+0 Hz</span></div>
      <input type="range" id="pitch" min="-30" max="30" step="5" value="0">
    </label>
    <label class="row" id="volumeRow">
      <div class="lbl"><span>音量</span><span class="val" id="volumeVal">+0%</span></div>
      <input type="range" id="volume" min="-50" max="50" step="5" value="0">
    </label>
    <div class="hint" id="sapiHint" style="display:none">
      Windows 自带引擎只认语速（会折算成它自己的 -10~10 档）；音调和音量在 SAPI 上不起作用。
    </div>
  </div>

  <div class="card">
    <h2>回复内容</h2>
    <label class="row">
      <div class="lbl"><span>Agent 干完活打电话时念的话</span></div>
      <textarea id="reportLine" spellcheck="false"></textarea>
    </label>
    <div class="hint">
      执行 <code>python say.py</code> 不带参数时，念的就是这一段。
      每条 Codex 总结想让电话先来一句这个，把环境变量 <code>VIBE_PHONE_PREFIX</code> 设成同样的内容。
    </div>
  </div>

  <div class="card">
    <div class="actions">
      <button class="primary" id="btnPreview" type="button">试听</button>
      <button class="primary" id="btnCall" type="button">立即拨一通</button>
      <button class="ghost" id="btnSave" type="button">保存</button>
      <button class="ghost" id="btnReload" type="button">重新读配置</button>
    </div>
    <div id="status">正在读取配置…</div>
    <audio id="player" controls preload="none"></audio>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let VOICES = {sapi: [], edge: []};
let SAVED = null;   // 上次保存/读取到的设置，用来判断"有没有改动"

function fmtPct(v){ return (v > 0 ? '+' : '') + v + '%'; }
function fmtHz(v){ return (v > 0 ? '+' : '') + v + ' Hz'; }
function onoff(v){ return v ? '开' : '关'; }

// ── 打电话开关：点一下就存，不进"保存"那一套 ──────────────────────────
function syncSwitchUI(sw){
  sw = sw || {};
  const a = sw.agent !== false, c = sw.codex !== false;
  $('swAgent').checked = a;
  $('swCodex').checked = c;
  $('swAgentRow').classList.toggle('off', !a);
  $('swCodexRow').classList.toggle('off', !c);
  const at = $('swAgentTag'), ct = $('swCodexTag');
  at.textContent = onoff(a); at.className = 'sw-tag ' + (a ? 'on' : 'off');
  ct.textContent = onoff(c); ct.className = 'sw-tag ' + (c ? 'on' : 'off');
}

async function pushSwitch(){
  const payload = {agent: $('swAgent').checked, codex: $('swCodex').checked};
  $('swAgent').disabled = $('swCodex').disabled = true;
  try {
    const res = await fetch('/api/switch', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!data.ok) { setStatus('开关没存上：' + (data.error || '未知错误'), 'err'); return; }
    if (SAVED) SAVED.call_switch = data.call_switch;
    syncSwitchUI(data.call_switch);
    setStatus('开关已生效（不用重启）：Agent ' + onoff(data.call_switch.agent) +
              ' · Codex ' + onoff(data.call_switch.codex), 'ok');
  } catch (e) {
    setStatus('开关没存上：' + e, 'err');
  } finally {
    $('swAgent').disabled = $('swCodex').disabled = false;
  }
}

function setStatus(text, kind){
  const el = $('status');
  el.textContent = text;
  el.className = kind || '';
}

function current(){
  return {
    backend: document.querySelector('#segBackend button.on').dataset.backend,
    voice: $('voice').value,
    rate: parseInt($('rate').value, 10),
    pitch: parseInt($('pitch').value, 10),
    volume: parseInt($('volume').value, 10),
    report_line: $('reportLine').value,
  };
}

function isDirty(){
  if (!SAVED) return false;
  const c = current();
  return c.backend !== SAVED.backend || c.voice !== (SAVED.voice || '') ||
         c.rate !== SAVED.rate || c.pitch !== SAVED.pitch ||
         c.volume !== SAVED.volume || c.report_line !== SAVED.report_line;
}

function markDirty(){
  if (isDirty()) {
    if (!/未保存/.test($('status').textContent)) setStatus('有改动未保存（点"保存"才会写进 config.json）', '');
    $('btnSave').classList.add('dirty');
  } else {
    $('btnSave').classList.remove('dirty');
  }
}

function fillVoices(){
  const backend = document.querySelector('#segBackend button.on').dataset.backend;
  const scope = $('localeFilter').value;
  const sel = $('voice');
  const keep = sel.value;

  let list = (VOICES[backend] || []).slice();
  if (backend === 'edge' && scope === 'zh') {
    list = list.filter(v => (v.locale || '').toLowerCase().startsWith('zh'));
  } else if (backend === 'edge') {
    list.sort((a, b) => (a.locale || '').localeCompare(b.locale || '') ||
                        (a.name || '').localeCompare(b.name || ''));
  }

  sel.innerHTML = '';
  if (!list.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = backend === 'edge' ? '（拿不到 Edge 音色列表）' : '（没有可用音色）';
    sel.appendChild(opt);
  }
  for (const v of list) {
    const opt = document.createElement('option');
    if (backend === 'sapi') {
      opt.value = v; opt.textContent = v;
    } else {
      opt.value = v.name;
      const g = v.gender === 'Male' ? '男' : (v.gender === 'Female' ? '女' : '');
      opt.textContent = v.name + '  · ' + g + ' · ' + v.locale;
    }
    sel.appendChild(opt);
  }
  $('voiceCount').textContent = list.length + ' 个';

  // 尽量保住用户原来选的音色；保不住就落到第一个（edge 用默认音色兜底）
  if (keep && list.some(v => (backend === 'sapi' ? v : v.name) === keep)) {
    sel.value = keep;
  } else if (backend === 'edge' && list.some(v => v.name === 'zh-CN-XiaoxiaoNeural')) {
    sel.value = 'zh-CN-XiaoxiaoNeural';
  }
}

function applyBackendUI(){
  const backend = document.querySelector('#segBackend button.on').dataset.backend;
  $('pitchRow').style.display = backend === 'edge' ? '' : 'none';
  $('volumeRow').style.display = backend === 'edge' ? '' : 'none';
  $('sapiHint').style.display = backend === 'sapi' ? '' : 'none';
  const edgeCount = (VOICES.edge || []).length;
  const h = $('backendHint');
  if (backend === 'edge') {
    if (edgeCount) {
      h.textContent = 'Edge 音色可用（共 ' + edgeCount + ' 个）。注意：每次合成都要联网，断网就没声音。';
      h.className = 'hint';
    } else {
      h.textContent = '拿不到 Edge 音色列表：可能没装 edge-tts / soundfile，或者断网了。';
      h.className = 'hint err';
    }
  } else {
    h.textContent = 'Windows 自带引擎：零安装、离线可用，但中文通常只有一个嗓子。';
    h.className = 'hint';
  }
  fillVoices();
}

function applySettings(cfg){
  SAVED = cfg;
  for (const btn of document.querySelectorAll('#segBackend button')) {
    btn.classList.toggle('on', btn.dataset.backend === (cfg.backend || 'sapi'));
  }
  $('rate').value = cfg.rate || 0;
  $('pitch').value = cfg.pitch || 0;
  $('volume').value = cfg.volume || 0;
  $('reportLine').value = cfg.report_line || '';
  $('rateVal').textContent = fmtPct(cfg.rate || 0);
  $('pitchVal').textContent = fmtHz(cfg.pitch || 0);
  $('volumeVal').textContent = fmtPct(cfg.volume || 0);
  syncSwitchUI(cfg.call_switch);
  applyBackendUI();
  if (cfg.voice) {
    const v = $('voice');
    if (Array.from(v.options).some(o => o.value === cfg.voice)) v.value = cfg.voice;
  }
  $('btnSave').classList.remove('dirty');
}

async function load(){
  setStatus('正在读取配置…');
  try {
    const cfg = await (await fetch('/api/console', {cache:'no-store'})).json();
    if (cfg.error) { setStatus('读配置失败：' + cfg.error, 'err'); return; }
    VOICES.sapi = cfg.sapi_voices || [];
    VOICES.edge = cfg.edge_voices || [];
    applySettings(cfg);
    setStatus('已就绪。引擎：' + (cfg.backend === 'edge' ? 'Edge 在线' : 'Windows 自带') +
              ' · 打电话开关：Agent ' + onoff((cfg.call_switch||{}).agent !== false) +
              ' / Codex ' + onoff((cfg.call_switch||{}).codex !== false), 'ok');
  } catch (e) {
    setStatus('读配置失败：' + e, 'err');
  }
}

async function preview(){
  const c = current();
  if (!c.report_line.trim()) { setStatus('先写点内容再试听。', 'err'); return; }
  $('btnPreview').disabled = true;
  setStatus('正在合成…（Edge 后端首次会慢一点）');
  try {
    const res = await fetch('/api/preview', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({...c, text: c.report_line})
    });
    const data = await res.json();
    if (!data.ok) { setStatus('合成失败：' + (data.error || '未知错误'), 'err'); return; }
    const player = $('player');
    player.src = data.url + '&t=' + Date.now();
    player.play().catch(() => {});
    setStatus('试听已生成，' + data.seconds + ' 秒。这就是话机听到的效果。', 'ok');
  } catch (e) {
    setStatus('合成失败：' + e, 'err');
  } finally {
    $('btnPreview').disabled = false;
  }
}

async function save(){
  const c = current();
  setStatus('正在保存…');
  try {
    const res = await fetch('/api/save', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(c)
    });
    const data = await res.json();
    if (!data.ok) { setStatus('保存失败：' + (data.error || '未知错误'), 'err'); return; }
    SAVED = Object.assign({}, c, {call_switch: SAVED ? SAVED.call_switch : undefined});
    $('btnSave').classList.remove('dirty');
    setStatus('已保存到 config.json，立刻生效（不用重启 bridge）。', 'ok');
  } catch (e) {
    setStatus('保存失败：' + e, 'err');
  }
}

async function callNow(){
  const c = current();
  if (!c.report_line.trim()) { setStatus('先写点内容再拨。', 'err'); return; }
  $('btnCall').disabled = true;
  setStatus('正在拨号…话机会先响 4 秒再接起来。（手动测试，不受开关限制）');
  try {
    const res = await fetch('/notify', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({...c, text: c.report_line, wait:false,
                            caller:'控制台', source:'manual'})
    });
    const data = await res.json();
    if (data.ok) setStatus('已拨出，去接电话（或等它自己接）。', 'ok');
    else setStatus('拨号失败：' + (data.error || '未知错误'), 'err');
  } catch (e) {
    setStatus('拨号失败：' + e, 'err');
  } finally {
    $('btnCall').disabled = false;
  }
}

for (const btn of document.querySelectorAll('#segBackend button')) {
  btn.addEventListener('click', () => {
    for (const b of document.querySelectorAll('#segBackend button')) b.classList.remove('on');
    btn.classList.add('on');
    applyBackendUI();
    markDirty();
  });
}
$('localeFilter').addEventListener('change', fillVoices);
$('swAgent').addEventListener('change', pushSwitch);
$('swCodex').addEventListener('change', pushSwitch);
$('voice').addEventListener('change', markDirty);
$('reportLine').addEventListener('input', markDirty);
for (const id of ['rate', 'pitch', 'volume']) {
  $(id).addEventListener('input', () => {
    const v = parseInt($(id).value, 10);
    $({rate:'rateVal', pitch:'pitchVal', volume:'volumeVal'}[id]).textContent =
      id === 'pitch' ? fmtHz(v) : fmtPct(v);
    markDirty();
  });
}
$('btnPreview').addEventListener('click', preview);
$('btnSave').addEventListener('click', save);
$('btnCall').addEventListener('click', callNow);
$('btnReload').addEventListener('click', load);
load();
</script>
</body>
</html>
"""
