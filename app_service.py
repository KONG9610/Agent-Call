"""Codex Phone desktop service. Local authenticated UI + durable sequential queue."""
import argparse
import copy
import datetime as dt
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import threading
import time
import uuid
import urllib.request
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from paths import DATA, ROOT, prepare
from monitor import Monitor

class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

DEFAULT = {
    'enabled': False, 'workbuddy_enabled': True, 'scope': 'all', 'selected_threads': [],
    'codex_home': str(Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))),
    'report_line': '高书记，我已经按照你的指示完成了工作。',
    'backend': 'sapi', 'voice': '', 'rate': 0, 'pitch': 0, 'volume': 0,
    'extension': '1001', 'caller': 'codex', 'sip_port': 5060, 'advertise_ip': '',
    'ring_seconds': 4, 'answer_timeout': 45, 'auto_answer_mode': 'two_stage',
    'legacy_api': 'http://127.0.0.1:8080',
}

def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)

def http_json(url, payload=None, timeout=5):
    req = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.load(r)

class App:
    def __init__(self):
        prepare()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.settings = copy.deepcopy(DEFAULT)
        if (DATA / 'settings.json').exists():
            self.settings.update(json.loads((DATA / 'settings.json').read_text(encoding='utf-8-sig')))
        self.log = logging.getLogger('codex-phone')
        self.log.setLevel(logging.INFO)
        handler = RotatingFileHandler(DATA / 'logs' / 'app.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        self.log.addHandler(handler)
        self.db = sqlite3.connect(DATA / 'queue.sqlite', check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, source TEXT, thread_id TEXT, at TEXT, status TEXT, detail TEXT)')
        # Never unexpectedly replay calls after a restart/crash.
        self.db.execute("UPDATE events SET status='cancelled',detail='程序重启，未自动重拨' WHERE status IN ('queued','calling')")
        self.db.commit()
        self.pbx = None
        self.bridge_mode = 'offline'
        self.bridge_error = ''
        self.last_call = ''
        self.monitor = Monitor(self.settings['codex_home'], self.enqueue, self.log.info)
        self.voice_lock = threading.Lock()
        self._voices = {'sapi': [], 'edge': [], 'loading': True, 'error': ''}
        self._setup_phone()
        threading.Thread(target=self._voice_list, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()

    def _setup_phone(self):
        import pbx, voice
        voice.CACHE_DIR = str(DATA / 'cache')
        try:
            status = http_json(self.settings['legacy_api'] + '/status')
            if 'registrations' in status and 'calls' in status and 'agent' in status:
                self.bridge_mode = 'external'
                self.log.info('复用正在运行的 Vibe Phone 服务；不会启动第二个 SIP 服务')
                return
        except Exception:
            pass
        try:
            cfg = copy.deepcopy(pbx.DEFAULT_CONFIG)
            cfg.update(agents={'6000': 'codex'}, uplink='off', registrar_ttl=120,
                       notify_menu=False, menu_wait_seconds=0,
                       auto_answer_mode=self.settings['auto_answer_mode'],
                       ring_seconds=self.settings['ring_seconds'], answer_timeout=self.settings['answer_timeout'])
            cfg['sip'].update(port=self.settings['sip_port'], advertise_ip=self.settings['advertise_ip'])
            # Preserve the tested phone headers; personal network details are not shipped.
            self.pbx = pbx.SIPServer(cfg, handler=None, log=self.log.info)
            self.pbx.start()
            self.bridge_mode = 'embedded'
        except Exception as exc:
            self.bridge_error = '电话服务启动失败（可能有程序占用 SIP 端口）：' + str(exc)
            self.log.error(self.bridge_error)
            self.pbx = None

    def _voice_list(self):
        import voice
        try:
            sapi = voice.list_voices()
            edge = voice.edge_voices(force=True)
            self._voices = {'sapi': sapi, 'edge': edge, 'loading': False,
                            'error': '' if edge else '暂时无法获取 Edge 音色，可使用本地音色或手填 Edge 音色名。'}
        except Exception as exc:
            self._voices.update(loading=False, error=str(exc))

    def save(self, data):
        with self.lock:
            new = copy.deepcopy(self.settings)
            for key in DEFAULT:
                if key in data:
                    new[key] = data[key]
            for key in ('enabled', 'workbuddy_enabled'):
                if not isinstance(new[key], bool):
                    raise ValueError('开关必须是布尔值')
            if new['scope'] not in ('all', 'selected') or new['backend'] not in ('sapi', 'edge'):
                raise ValueError('无效的监听范围或语音引擎')
            if new['auto_answer_mode'] not in ('two_stage', 'header', 'manual'):
                raise ValueError('无效的接听模式')
            if not isinstance(new['selected_threads'], list) or not all(isinstance(x, str) for x in new['selected_threads']):
                raise ValueError('任务选择格式不正确')
            for key in ('report_line', 'caller', 'extension', 'codex_home', 'advertise_ip', 'voice'):
                if not isinstance(new[key], str):
                    raise ValueError(key + ' 必须是文本')
            new['report_line'] = new['report_line'].strip()
            if not new['report_line'] or len(new['report_line']) > 1000:
                raise ValueError('播报文案应为 1–1000 字')
            if not new['extension'].isdigit() or not new['caller'].strip() or any(c in new['caller'] for c in '\r\n"'):
                raise ValueError('分机号应为数字；来电名称不能含引号或换行')
            for key in ('rate', 'pitch', 'volume'):
                new[key] = max(-100, min(100, int(new[key])))
            new['sip_port'] = int(new['sip_port'])
            if not 1024 <= new['sip_port'] <= 65535:
                raise ValueError('SIP 端口必须介于 1024–65535')
            new['ring_seconds'] = max(0, min(30, int(new['ring_seconds'])))
            new['answer_timeout'] = max(10, min(120, int(new['answer_timeout'])))
            # UI cannot redirect notifications to an arbitrary remote HTTP service.
            from urllib.parse import urlparse
            parsed = urlparse(new['legacy_api'])
            if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1':
                raise ValueError('旧服务地址必须为本机 127.0.0.1')
            changed_home = new['codex_home'] != self.settings['codex_home']
            restart = any(new[k] != self.settings[k] for k in ('sip_port', 'advertise_ip', 'legacy_api'))
            self.settings = new
            atomic_json(DATA / 'settings.json', new)
            if changed_home:
                self.monitor = Monitor(new['codex_home'], self.enqueue, self.log.info)
            self.db.execute("UPDATE events SET status='cancelled', detail='通知开关关闭或监听范围改变' WHERE status='queued' AND source != 'manual' AND (?=0 OR (source='workbuddy' AND ?=0))", (new['enabled'], new['workbuddy_enabled']))
            if new['scope'] == 'selected':
                for event_id, tid in self.db.execute("SELECT id,thread_id FROM events WHERE status='queued' AND source='codex'").fetchall():
                    if tid not in new['selected_threads']:
                        self.db.execute("UPDATE events SET status='cancelled',detail='任务已取消选择' WHERE id=?", (event_id,))
            self.db.commit()
            return {'ok': True, 'restart_required': restart}

    def enqueue(self, event):
        source = event.get('source', 'workbuddy')
        if source not in ('codex', 'workbuddy', 'manual'):
            raise ValueError('不支持的通知来源')
        tid = str(event.get('thread_id', ''))[:100]
        turn = str(event.get('turn_id') or uuid.uuid4().hex)[:100]
        event_id = source + ':' + tid + ':' + turn
        with self.lock:
            cfg = self.settings
            allowed = source == 'manual' or (cfg['enabled'] and
                       (source != 'workbuddy' or cfg['workbuddy_enabled']) and
                       (source != 'codex' or cfg['scope'] == 'all' or tid in cfg['selected_threads']))
            state = 'queued' if allowed else 'skipped'
            try:
                self.db.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',
                                (event_id, source, tid, dt.datetime.now().isoformat(timespec='seconds'), state,
                                 '' if allowed else '开关关闭或任务不在监听范围'))
                self.db.commit()
            except sqlite3.IntegrityError:
                return {'ok': True, 'duplicate': True}
            self.log.info('事件 %s %s %s', source, state, tid)
            return {'ok': True, 'queued': allowed, 'skipped': not allowed, 'event_id': event_id,
                    'reason': '' if allowed else '通知开关关闭或任务不在监听范围'}

    def _watch(self):
        while not self.stop.is_set():
            try:
                with self.lock:
                    self.monitor.poll()
                for path in (DATA / 'requests').glob('*.json'):
                    try:
                        event = json.loads(path.read_text(encoding='utf-8'))
                        # Calls requested while the app was off must not suddenly ring on startup.
                        if float(event.get('created', 0)) >= self.monitor.started:
                            self.enqueue(event)
                        path.unlink()
                    except Exception as exc:
                        self.log.warning('忽略无效通知文件 %s: %s', path.name, exc)
                        path.rename(path.with_suffix('.invalid'))
            except Exception as exc:
                self.log.exception('监听失败: %s', exc)
            self.stop.wait(0.5)

    def _worker(self):
        while not self.stop.is_set():
            with self.lock:
                row = self.db.execute("SELECT id,source FROM events WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                if row:
                    cfg = copy.deepcopy(self.settings)
                    self.db.execute("UPDATE events SET status='calling' WHERE id=?", (row[0],))
                    self.db.commit()
            if not row:
                self.stop.wait(0.2)
                continue
            try:
                result = self.call(cfg, row[1])
                if result.get('cancelled'):
                    status, detail = 'cancelled', result.get('reason', '通知已取消')
                elif not result.get('ok'):
                    raise RuntimeError(result.get('error') or result.get('reason') or '拨号失败')
                else:
                    status, detail = 'done', '已播报并结束通话'
            except Exception as exc:
                status, detail = 'failed', str(exc)
                self.log.exception('拨号失败')
            with self.lock:
                self.last_call = detail
                self.db.execute('UPDATE events SET status=?,detail=? WHERE id=?', (status, detail, row[0]))
                self.db.commit()

    def call(self, cfg, source='manual'):
        import voice, pbx
        with self.voice_lock:
            # Warm cache before ringing so a failed network TTS never causes an empty phone call.
            voice.synthesize(cfg['report_line'], backend=cfg['backend'], voice=cfg['voice'] or None,
                             rate=cfg['rate'], pitch=cfg['pitch'], volume=cfg['volume'])
            with self.lock:
                if self.stop.is_set() or (source != 'manual' and (not self.settings['enabled'] or
                        (source == 'workbuddy' and not self.settings['workbuddy_enabled']))):
                    return {'ok': False, 'cancelled': True, 'reason': '拨号前通知已关闭，已取消'}
            payload = {'text': cfg['report_line'], 'extension': cfg['extension'], 'caller': cfg['caller'],
                       'backend': cfg['backend'], 'voice': cfg['voice'], 'rate': cfg['rate'],
                       'pitch': cfg['pitch'], 'volume': cfg['volume'], 'source': 'manual', 'wait': True,
                       'ring': cfg['ring_seconds'], 'timeout': cfg['answer_timeout']}
            if self.bridge_mode == 'external':
                return http_json(cfg['legacy_api'] + '/notify', payload, timeout=240)
            if self.pbx is None:
                raise RuntimeError(self.bridge_error)
            self.pbx.cfg['auto_answer_mode'] = cfg['auto_answer_mode']
            return pbx.notify(self.pbx, payload, wait=True)

    def preview(self, payload):
        import voice, rtp
        with self.voice_lock:
            text = str(payload.get('report_line') or self.settings['report_line'])[:1000]
            pcm = voice.synthesize(text, backend=payload.get('backend', 'sapi'), voice=payload.get('voice') or None,
                                   rate=int(payload.get('rate', 0)), pitch=int(payload.get('pitch', 0)),
                                   volume=int(payload.get('volume', 0)))
            path = DATA / 'cache' / 'preview.wav'
            rtp.write_wav_pcm(str(path), pcm)
            return {'ok': True, 'path': str(path)}

    def threads(self):
        home = Path(self.settings['codex_home'])
        for path in sorted(home.glob('state_*.sqlite'), reverse=True):
            try:
                db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
                try:
                    rows = db.execute('SELECT id,title,cwd FROM threads ORDER BY updated_at DESC LIMIT 300').fetchall()
                    return [{'id': x[0], 'title': (x[1] or x[0])[:160], 'cwd': (x[2] or '')[:2048]} for x in rows]
                finally:
                    db.close()
            except sqlite3.Error:
                pass
        found = {}
        try:
            for line in (home / 'session_index.jsonl').read_text(encoding='utf-8').splitlines():
                try:
                    row = json.loads(line)
                    found[row['id']] = {'id': row['id'], 'title': (row.get('thread_name') or row['id'])[:160], 'cwd': ''}
                except (ValueError, KeyError):
                    continue
        except OSError:
            pass
        return list(found.values())[-300:][::-1]

    def state(self):
        try:
            phone = self.pbx.status() if self.pbx else (http_json(self.settings['legacy_api'] + '/status', timeout=2) if self.bridge_mode == 'external' else {})
        except Exception as exc:
            phone = {'error': str(exc)}
        with self.lock:
            history = self.db.execute('SELECT source,thread_id,at,status,detail FROM events ORDER BY rowid DESC LIMIT 60').fetchall()
            pending = self.db.execute("SELECT count(*) FROM events WHERE status='queued'").fetchone()[0]
            active = self.db.execute("SELECT count(*) FROM events WHERE status='calling'").fetchone()[0]
            ips = []
            try:
                ips = sorted({x[4][0] for x in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET) if not x[4][0].startswith('127.')})
            except OSError:
                pass
            return {'settings': self.settings, 'bridge_mode': self.bridge_mode, 'bridge_error': self.bridge_error,
                    'phone': phone, 'ips': ips, 'pending': pending, 'active': active,
                    'last_call': self.last_call, 'monitor_error': self.monitor.error,
                    'last_event': self.monitor.last_event, 'watching_files': len(self.monitor.files),
                    'voices': self._voices, 'data_dir': str(DATA),
                    'history': [dict(zip(('source', 'thread_id', 'at', 'status', 'detail'), r)) for r in history]}

    def shutdown(self):
        self.stop.set()
        if self.pbx:
            self.pbx.stop()

def serve():
    # An exclusive loopback guard also prevents double service starts.
    prepare()
    guard = socket.socket()
    if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
        guard.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        guard.bind(('127.0.0.1', int(os.environ.get('CODEX_PHONE_GUARD_PORT', '18764'))))
        guard.listen(1)
    except OSError:
        raise SystemExit('Codex Phone is already running, or port 18764 is occupied.')
    app = App()
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, code, value):
            body = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def authorized(self):
            return secrets.compare_digest(self.headers.get('X-Phone-Token', ''), token)
        def do_GET(self):
            if not self.authorized():
                return self.reply(403, {'error': 'Unauthorized'})
            try:
                if self.path == '/state':
                    return self.reply(200, app.state())
                if self.path == '/threads':
                    return self.reply(200, app.threads())
                return self.reply(404, {'error': 'Not found'})
            except Exception as exc:
                self.reply(500, {'error': str(exc)})
        def do_POST(self):
            if not self.authorized():
                return self.reply(403, {'error': 'Unauthorized'})
            try:
                length = int(self.headers.get('Content-Length', 0))
                if not 0 <= length <= 65536:
                    raise ValueError('请求过大')
                data = json.loads(self.rfile.read(length) or b'{}')
                if self.path == '/settings':
                    result = app.save(data)
                elif self.path == '/test':
                    result = app.enqueue({'source': 'manual'})
                elif self.path == '/preview':
                    result = app.preview(data)
                elif self.path == '/refresh-voices':
                    threading.Thread(target=app._voice_list, daemon=True).start()
                    result = {'ok': True}
                elif self.path == '/shutdown':
                    self.reply(200, {'ok': True})
                    app.stop.set()
                    return
                else:
                    return self.reply(404, {'error': 'Not found'})
                self.reply(200, result)
            except Exception as exc:
                self.reply(400, {'error': str(exc)})
    server = LocalHTTPServer(('127.0.0.1', 0), Handler)
    legacy = None
    if app.bridge_mode == 'embedded':
        class LegacyHandler(BaseHTTPRequestHandler):
            """Compatibility for existing local say.py / WorkBuddy memory calls."""
            def log_message(self, *args):
                pass
            def reply(self, code, data):
                body = json.dumps(data, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                if self.path == '/status':
                    state = app.pbx.status()
                    state['call_switch'] = {'agent': app.settings['enabled'] and app.settings['workbuddy_enabled'],
                                            'codex': app.settings['enabled']}
                    return self.reply(200, state)
                self.reply(404, {'error': 'Open CodexPhone.exe for settings'})
            def do_POST(self):
                if self.path != '/notify':
                    return self.reply(404, {'error': 'Not found'})
                # Legacy trusted-local scripts have no Origin header; reject browser-originated writes.
                if self.headers.get('Origin') or self.headers.get_content_type() != 'application/json':
                    return self.reply(403, {'error': 'Local JSON scripts only'})
                try:
                    length = int(self.headers.get('Content-Length', 0))
                    if not 0 <= length <= 65536:
                        raise ValueError('Request too large')
                    payload = json.loads(self.rfile.read(length))
                    source = payload.get('source', 'agent')
                    source = 'workbuddy' if source == 'agent' else source
                    result = app.enqueue({'source': source, 'thread_id': payload.get('thread_id', ''),
                                          'turn_id': payload.get('turn_id')})
                    if result.get('skipped'):
                        result['ok'] = False
                        return self.reply(200, result)
                    if not payload.get('wait', True):
                        return self.reply(200, result)
                    deadline = time.time() + 240
                    while time.time() < deadline and not app.stop.wait(.15):
                        with app.lock:
                            row = app.db.execute('SELECT status,detail FROM events WHERE id=?', (result['event_id'],)).fetchone()
                        if row and row[0] not in ('queued', 'calling'):
                            return self.reply(200, {'ok': row[0] == 'done', 'error': row[1] if row[0] != 'done' else None,
                                                    'text': app.settings['report_line'], 'status': row[0]})
                    self.reply(200, {'ok': True, 'queued': True, 'reason': 'Still queued; inspect desktop history'})
                except Exception as exc:
                    self.reply(400, {'ok': False, 'error': str(exc)})
        try:
            from urllib.parse import urlparse
            legacy_port = urlparse(app.settings['legacy_api']).port or 8080
            if legacy_port > 1023:
                legacy = LocalHTTPServer(('127.0.0.1', legacy_port), LegacyHandler)
                threading.Thread(target=legacy.serve_forever, daemon=True).start()
                app.log.info('旧脚本兼容入口: http://127.0.0.1:%s/notify', legacy_port)
        except OSError as exc:
            app.log.warning('旧脚本兼容入口未启用: %s', exc)
    atomic_json(DATA / 'runtime.json', {'port': server.server_port, 'token': token, 'pid': os.getpid()})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while not app.stop.wait(0.5):
            pass
    finally:
        app.shutdown()
        server.shutdown()
        server.server_close()
        if legacy:
            legacy.shutdown()
            legacy.server_close()
        (DATA / 'runtime.json').unlink(missing_ok=True)
        guard.close()

def request_call(source):
    prepare()
    # Do not claim delivery if the desktop application is not alive.
    try:
        runtime = json.loads((DATA / 'runtime.json').read_text(encoding='utf-8'))
        req = urllib.request.Request('http://127.0.0.1:%s/state' % runtime['port'], headers={'X-Phone-Token': runtime['token']})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=4) as r:
            state = json.load(r)
        if not state['settings']['enabled'] or (source == 'workbuddy' and not state['settings']['workbuddy_enabled']):
            print('SKIPPED: notification switch is off')
            return 0
        atomic_json(DATA / 'requests' / (uuid.uuid4().hex + '.json'), {'source': source, 'created': time.time()})
        print('QUEUED: request accepted; see application history for call result')
        return 0
    except Exception as exc:
        print('ERROR: start CodexPhone.exe first. ' + str(exc))
        return 1

def phone_setup(args):
    """Optional AI-friendly Yealink adapter. Never print credentials or replace active accounts silently."""
    import getpass
    from yealink_web import YealinkPhone
    password = os.environ.get('CODEX_PHONE_ADMIN_PASSWORD') or getpass.getpass('Phone web admin password: ')
    phone = YealinkPhone(args.phone_ip, user=args.phone_user, pwd=password)
    phone.login()
    before = phone.show_account(args.account)
    safe = ['AccountEnable', 'AccountLabel', 'AccountDisplayName', 'AccountRegisterName',
            'AccountUserName', 'server1', 'port1', 'transport1', 'AccountStunSwitch', 'AccountOutboundSwitch']
    if args.phone_configure:
        if not args.server_ip:
            raise ValueError('--server-ip is required')
        if before.get('AccountEnable') == '1' and before.get('server1') not in ('', args.server_ip) and not args.replace_account:
            raise ValueError('This account points at another server. Choose an unused account, or explicitly use --replace-account after owner approval.')
        phone.set_account(args.account, args.extension, args.server_ip, args.sip_port, label='codex', expires=60)
    after = phone.show_account(args.account)
    print(json.dumps({k: after.get(k) for k in safe}, ensure_ascii=True, indent=2))
    if args.phone_configure and (after.get('server1') != args.server_ip or after.get('AccountUserName') != args.extension):
        raise ValueError('Phone configuration readback did not match')
    return 0

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--call', action='store_true')
    parser.add_argument('--source', choices=['workbuddy'], default='workbuddy')
    parser.add_argument('--phone-ip', help='Read a Yealink account without exposing its password')
    parser.add_argument('--phone-user', default='admin')
    parser.add_argument('--account', type=int, default=0)
    parser.add_argument('--phone-configure', action='store_true')
    parser.add_argument('--replace-account', action='store_true')
    parser.add_argument('--server-ip')
    parser.add_argument('--extension', default='1001')
    parser.add_argument('--sip-port', type=int, default=5060)
    args = parser.parse_args()
    if args.phone_ip:
        sys.exit(phone_setup(args))
    if args.call:
        sys.exit(request_call(args.source))
    serve()
