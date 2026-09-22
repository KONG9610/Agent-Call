"""Regression tests for event boundaries, duplicate suppression, switches and queue order."""
import copy
import datetime as dt
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch
import app_service as service
from monitor import Monitor

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / 'sessions').mkdir()
        self.path = self.home / 'sessions' / 'rollout-a.jsonl'
        self.path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'thread-a', 'cwd': 'D:/demo'}}) + '\n')
        self.now = time.time()
        self.events = []
    def tearDown(self):
        self.tmp.cleanup()
    def append(self, kind, turn='turn-a', stamp=None):
        row = {'timestamp': dt.datetime.fromtimestamp(stamp or self.now, dt.timezone.utc).isoformat(),
               'type': 'event_msg', 'payload': {'type': kind, 'turn_id': turn}}
        with self.path.open('a') as f: f.write(json.dumps(row) + '\n')
    def test_baseline_progress_abort_and_only_completed(self):
        self.append('task_complete', 'old')
        m = Monitor(self.home, self.events.append, clock=lambda: self.now)
        self.append('task_started'); self.append('agent_message'); self.append('turn_aborted'); m.poll()
        self.assertEqual(self.events, [])
        self.append('task_complete'); m.poll(); m.poll()
        self.assertEqual([e['turn_id'] for e in self.events], ['turn-a'])
    def test_partial_json_line_and_unicode(self):
        m = Monitor(self.home, self.events.append, clock=lambda: self.now)
        row = json.dumps({'timestamp': dt.datetime.fromtimestamp(self.now,dt.timezone.utc).isoformat(),
                         'type':'event_msg','payload':{'type':'task_complete','turn_id':'中文'}},ensure_ascii=False).encode()
        with self.path.open('ab') as f: f.write(row[:70])
        m.poll(); self.assertEqual(self.events, [])
        with self.path.open('ab') as f: f.write(row[70:]+b'\n')
        m.poll(); self.assertEqual(self.events[0]['turn_id'],'中文')
    def test_new_file_old_completion_not_replayed(self):
        m = Monitor(self.home, self.events.append, clock=lambda: self.now)
        self.path = self.home / 'sessions' / 'rollout-b.jsonl'
        self.path.write_text(json.dumps({'type':'session_meta','payload':{'id':'thread-b'}})+'\n')
        self.append('task_complete','old',self.now-60)
        self.now += 4; m.poll(); self.assertEqual(self.events,[])
        self.append('task_complete','new'); m.poll(); self.assertEqual(self.events[0]['thread_id'],'thread-b')
    def test_subagent_not_notified(self):
        self.path.write_text(json.dumps({'type':'session_meta','payload':{'id':'child','source':{'subagent':{}}}})+'\n')
        m=Monitor(self.home,self.events.append,clock=lambda:self.now)
        self.append('task_complete');m.poll();self.assertEqual(self.events,[])

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.data=Path(self.tmp.name)
        self.patcher=patch.object(service,'DATA',self.data);self.patcher.start()
        self.app=service.App.__new__(service.App)
        self.app.lock=threading.RLock();self.app.settings=copy.deepcopy(service.DEFAULT)
        self.app.log=logging.getLogger('test');self.app.log.addHandler(logging.NullHandler());self.app.log.propagate=False
        self.app.stop=threading.Event();self.app.last_call=''
        self.app.db=sqlite3.connect(':memory:',check_same_thread=False)
        self.app.db.execute('CREATE TABLE events (id TEXT PRIMARY KEY, source TEXT, thread_id TEXT, at TEXT, status TEXT, detail TEXT)')
    def tearDown(self):
        self.app.stop.set(); self.app.db.close();self.patcher.stop();self.tmp.cleanup()
    def event(self,t='a',turn='1',source='codex'):
        return {'source':source,'thread_id':t,'turn_id':turn}
    def test_off_ignores_then_on_does_not_replay(self):
        self.assertTrue(self.app.enqueue(self.event())['skipped'])
        self.app.settings['enabled']=True
        self.assertTrue(self.app.enqueue(self.event())['duplicate'])
        self.assertTrue(self.app.enqueue(self.event(turn='2'))['queued'])
    def test_selected_scope_and_workbuddy_switch(self):
        self.app.settings.update(enabled=True,scope='selected',selected_threads=['a'],workbuddy_enabled=False)
        self.assertTrue(self.app.enqueue(self.event('b'))['skipped'])
        self.assertTrue(self.app.enqueue(self.event('a'))['queued'])
        self.assertTrue(self.app.enqueue(self.event(source='workbuddy'))['skipped'])
        self.assertTrue(self.app.enqueue(self.event(source='manual'))['queued'])
    def test_switch_off_cancels_pending(self):
        self.app.settings['enabled']=True; self.app.enqueue(self.event())
        self.app.save({'enabled':False})
        self.assertEqual(self.app.db.execute('SELECT status FROM events').fetchone()[0],'cancelled')
    def test_duplicate_and_fifo_and_error_does_not_block_next(self):
        self.app.settings['enabled']=True
        for i in range(3): self.app.enqueue(self.event(turn=str(i)))
        self.assertTrue(self.app.enqueue(self.event(turn='1'))['duplicate'])
        calls=[]
        def call(cfg,source):
            calls.append(source)
            if len(calls)==1: raise RuntimeError('phone offline')
            if len(calls)==3:self.app.stop.set()
            return {'ok':True}
        self.app.call=call
        self.app._worker()
        self.assertEqual(len(calls),3)
        self.assertEqual([r[0] for r in self.app.db.execute('SELECT status FROM events ORDER BY rowid')],['failed','done','done'])
    def test_input_validation(self):
        for value in ({'report_line':''},{'caller':'bad\r\nVia: x'},{'sip_port':0},{'legacy_api':'http://example.com'}):
            with self.assertRaises(ValueError):self.app.save(value)
    def test_oversized_task_titles_cannot_break_desktop_response(self):
        self.app.settings['codex_home']=str(self.data)
        db=sqlite3.connect(self.data/'state_5.sqlite')
        db.execute('CREATE TABLE threads(id TEXT,title TEXT,cwd TEXT,updated_at INTEGER)')
        db.execute('INSERT INTO threads VALUES (?,?,?,?)',('a','很长的任务标题'*20000,'D:/demo',1));db.commit();db.close()
        rows=self.app.threads()
        self.assertEqual(len(rows[0]['title']),160)

class PortTests(unittest.TestCase):
    def test_control_port_cannot_be_shared(self):
        first=service.LocalHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)
        try:
            with self.assertRaises(OSError):
                second=service.LocalHTTPServer(first.server_address,BaseHTTPRequestHandler)
                second.server_close()
        finally:first.server_close()

if __name__=='__main__':unittest.main()
