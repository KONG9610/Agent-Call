"""Read only Codex's local completion records; never infer completion from silence.

This is a version-sensitive desktop adapter, not an official stable public API.
Only records appended while the app runs are considered; old completions are skipped.
"""
import datetime as dt
import json
import re
import time
from pathlib import Path

class Monitor:
    def __init__(self, home, callback, log=lambda x: None, clock=time.time):
        self.home = Path(home)
        self.callback, self.log, self.clock = callback, log, clock
        self.started = clock()
        self.files = {}
        self.discovered = 0
        self.last_event = ''
        self.error = ''
        self._discover(baseline=True)

    def _meta(self, path):
        match = re.search(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$', path.name)
        meta = {'id': match.group(1) if match else '', 'cwd': '', 'subagent': False}
        try:
            with path.open('rb') as f:
                row = json.loads(f.readline(1024 * 1024))
            if row.get('type') == 'session_meta':
                p = row.get('payload', {})
                meta.update(id=p.get('id', meta['id']), cwd=p.get('cwd', ''),
                            subagent=isinstance(p.get('source'), dict) and 'subagent' in p['source'])
        except (OSError, ValueError):
            pass
        return meta

    def _discover(self, baseline=False):
        root = self.home / 'sessions'
        if not root.is_dir():
            self.error = '未找到 Codex sessions 目录，请设置正确的 Codex 数据目录。'
            return
        self.error = ''
        for path in root.rglob('*.jsonl'):
            if path not in self.files:
                try:
                    self.files[path] = {'offset': path.stat().st_size if baseline else 0,
                                        'tail': b'', 'meta': self._meta(path)}
                except OSError:
                    continue
        self.discovered = self.clock()

    def poll(self):
        if self.clock() - self.discovered >= 3:
            self._discover()
        for path, state in list(self.files.items()):
            try:
                size = path.stat().st_size
                if size < state['offset']:
                    state.update(offset=0, tail=b'')
                if size == state['offset']:
                    continue
                with path.open('rb') as f:
                    f.seek(state['offset'])
                    chunk = f.read(8 * 1024 * 1024)
                    state['offset'] = f.tell()
                lines = (state['tail'] + chunk).split(b'\n')
                state['tail'] = lines.pop()
                if len(state['tail']) > 32 * 1024 * 1024:
                    state['tail'] = b''
                    self.log('跳过过大的任务记录行')
                for line in lines:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get('type') == 'session_meta':
                        state['meta'] = self._meta(path)
                    p = row.get('payload') or {}
                    if row.get('type') != 'event_msg' or p.get('type') != 'task_complete':
                        continue
                    if state['meta']['subagent']:
                        continue
                    stamp = row.get('timestamp', '')
                    try:
                        at = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
                    except (ValueError, TypeError):
                        continue
                    # Codex serializes timestamps at millisecond precision.
                    if at + 0.001 < self.started or not p.get('turn_id') or not state['meta']['id']:
                        continue
                    self.last_event = stamp
                    self.callback({'source': 'codex', 'thread_id': state['meta']['id'],
                                   'turn_id': p['turn_id'], 'cwd': state['meta']['cwd'], 'at': stamp})
            except OSError as exc:
                self.error = str(exc)
