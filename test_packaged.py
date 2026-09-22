"""Integration test of the shipped service against a loopback fake phone.

Creates its own data/ports; never calls a physical phone. Optionally tests live Edge
with --edge (sends only the fixed test phrase to the speech service).
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import selftest
import sipcore as sip
import pbx

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--edge',action='store_true');args=parser.parse_args()
    root=Path(__file__).parent
    data=root/'.build-tmp'/('integration-'+uuid.uuid4().hex[:8]);data.mkdir(parents=True)
    home=data/'codex';(home/'sessions').mkdir(parents=True)
    settings={'enabled':True,'sip_port':15064,'advertise_ip':'127.0.0.1','legacy_api':'http://127.0.0.1:18089',
              'codex_home':str(home),'report_line':'测试完成。','ring_seconds':0,'backend':'sapi'}
    (data/'settings.json').write_text(json.dumps(settings),encoding='utf-8')
    env=dict(os.environ,CODEX_PHONE_DATA=str(data),CODEX_PHONE_GUARD_PORT='18765')
    exe=root/'dist'/'PhoneService'/'PhoneService.exe'
    log=(data/'service-output.txt').open('w',encoding='utf-8')
    proc=subprocess.Popen([str(exe)],env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    stop=threading.Event(); phone=None
    checks=[]
    def check(name,condition):
        if not condition:raise AssertionError(name)
        checks.append(name);print('PASS '+name,flush=True)
    runtime=None
    def api(path,body=None,authorized=True):
        headers={'Content-Type':'application/json'}
        if authorized:headers['X-Phone-Token']=runtime['token']
        req=urllib.request.Request('http://127.0.0.1:%d%s'%(runtime['port'],path),
            data=None if body is None else json.dumps(body).encode(),headers=headers)
        with opener.open(req,timeout=100) as r:return json.load(r)
    def wait_for(predicate,timeout=45):
        deadline=time.time()+timeout
        while time.time()<deadline:
            value=predicate()
            if value:return value
            time.sleep(.25)
        raise AssertionError('Timed out waiting for test condition')
    try:
        wait_for(lambda:(data/'runtime.json').exists(),20)
        runtime=json.loads((data/'runtime.json').read_text(encoding='utf-8'))
        check('packaged service starts embedded SIP',api('/state')['bridge_mode']=='embedded')
        try:api('/state',authorized=False);check('unauthorized request blocked',False)
        except urllib.error.HTTPError as ex:check('unauthorized request blocked',ex.code==403)
        selftest.SIP_PORT=15064
        phone=selftest.FakePhone();reply,_=phone.register()
        check('fake phone registers',reply is not None and sip.status_of(reply[0])==200)
        evidence={'invites':0,'byes':0,'rtp':0,'names':[]}
        def responder():
            while not stop.is_set():
                try:msg,addr=selftest.recv_sip(phone.sock,.3)
                except OSError:return
                if not msg:continue
                start,headers,body=msg
                if start.startswith('INVITE'):
                    evidence['invites']+=1;evidence['names'].append(sip.header_get(headers,'from'))
                    phone.answer_invite({'start':start,'headers':headers,'body':body,'addr':addr})
                elif start.startswith('BYE'):
                    evidence['byes']+=1
                    phone.sock.sendto(sip.build_response('',headers,200,'OK'),addr)
        def rtp_reader():
            while not stop.is_set():
                try:
                    raw,_=phone.rtp.recvfrom(4096)
                    if len(raw)>12:evidence['rtp']+=1
                except socket.timeout:pass
                except OSError:return
        threading.Thread(target=responder,daemon=True).start();threading.Thread(target=rtp_reader,daemon=True).start()
        path=home/'sessions'/'test.jsonl'
        path.write_text(json.dumps({'type':'session_meta','payload':{'id':'integration-thread','cwd':str(data)}})+'\n')
        def event(turn,kind='task_complete'):
            with path.open('a',encoding='utf-8') as f:f.write(json.dumps({'timestamp':dt.datetime.now(dt.timezone.utc).isoformat(),
                'type':'event_msg','payload':{'type':kind,'turn_id':turn}})+'\n')
        event('progress','agent_message');time.sleep(3.5)
        check('progress does not enqueue',not api('/state')['history'])
        event('one');event('one');event('two')
        state=wait_for(lambda:(lambda s:s if len([x for x in s['history'] if x['status']=='done'])==2 else None)(api('/state')),70)
        check('two completion events call sequentially and duplicate suppressed',len(state['history'])==2 and evidence['byes']==2)
        check('audio packets and codex caller sent',evidence['rtp']>40 and all('codex' in name for name in evidence['names']))
        api('/settings',{'enabled':False});event('off')
        wait_for(lambda:any(x['status']=='skipped' for x in api('/state')['history']))
        check('disabled completion is skipped',evidence['byes']==2)
        api('/settings',{'enabled':True,'scope':'selected','selected_threads':['another-thread']});event('unselected')
        wait_for(lambda:len(api('/state')['history'])==4)
        check('unselected task is skipped',api('/state')['history'][0]['status']=='skipped')
        result=subprocess.run([str(exe),'--call','--source','workbuddy'],env=env,capture_output=True,text=True,timeout=15)
        check('WorkBuddy command queues without touching Codex settings','QUEUED' in result.stdout and result.returncode==0)
        wait_for(lambda:len([x for x in api('/state')['history'] if x['status']=='done'])==3,50)
        check('WorkBuddy call completed',evidence['byes']==3)
        legacy_req=urllib.request.Request('http://127.0.0.1:18089/notify',data=json.dumps({'source':'agent','text':'old caller text','wait':False}).encode(),headers={'Content-Type':'application/json'})
        with opener.open(legacy_req,timeout=5) as response: legacy_result=json.load(response)
        check('legacy say.py interface joins the same queue',legacy_result['queued'])
        wait_for(lambda:len([x for x in api('/state')['history'] if x['status']=='done'])==4,50)
        check('legacy WorkBuddy call completed',evidence['byes']==4)
        if args.edge:
            out=api('/preview',{'report_line':'这是网络音色测试。','backend':'edge','voice':'zh-CN-YunxiNeural','rate':0,'pitch':0,'volume':0})
            check('packaged Edge synthesis produces WAV',Path(out['path']).stat().st_size>16000)
        api('/shutdown',{});proc.wait(10)
        check('clean shutdown removes runtime token',not (data/'runtime.json').exists())
        (data/'result.json').write_text(json.dumps({'checks':checks,'evidence':evidence},ensure_ascii=False,indent=2),encoding='utf-8')
        print('REPORT '+str(data/'result.json'),flush=True)
    finally:
        stop.set()
        if proc.poll() is None:
            try:api('/shutdown',{});proc.wait(10)
            except Exception:proc.terminate();proc.wait(10)
        if phone:
            phone.sock.close();phone.rtp.close()
        log.close()

if __name__=='__main__':main()
