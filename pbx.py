"""pbx.py - a tiny SIP registrar + call controller for the Vibe Phone project.

Four jobs, nothing more:
  * accept REGISTER so an IP phone becomes reachable
  * place an outbound call (INVITE) and stream audio to the phone
  * accept an inbound call, answer it, and capture what the caller says
  * run the RTP side of the call (G.711, 20 ms frames)

No digest authentication: the server is meant to live on your own LAN.
"""

import glob
import json
import os
import re
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import console
import rtp
import sipcore as sip
import voice

HERE = os.path.dirname(os.path.abspath(__file__))

# 控制台"试听"生成的音频落在这里。只留最近几个，免得越堆越多。
PREVIEW_DIR = os.path.join(HERE, "cache", "preview")
PREVIEW_KEEP = 8

DEFAULT_CONFIG = {
    "sip": {
        "listen": "0.0.0.0",
        "port": 5060,
        "advertise_ip": "",
    },
    "http": {
        "listen": "127.0.0.1",
        "port": 8080,
    },
    "agents": {
        "6000": "WorkBuddy"
    },
    "default_extension": "1001",
    "uplink": "soundcard",
    "max_call_seconds": 900,
    "soundcard": {
        "device": "",
        "gain": 2.0,
        "greeting": "请讲。",
        "max_call_seconds": 900
    },
    "codecs": ["PCMA", "PCMU"],
    "rtp_ports": [16384, 16500],
    "ring_seconds": 4,
    "registrar_ttl": 120,
    "invite_headers": [
        "Alert-Info: <http://127.0.0.1>;info=alert-autoanswer",
        "Call-Info: <sip:127.0.0.1>;answer-after=0"
    ],
    "voice": {
        # "sapi" = Windows 自带引擎（离线、零安装）；"edge" = Edge 在线音色（要联网）
        "backend": "sapi",
        "tts_voice": "",
        "rate": 0,
        "pitch": 0,
        "volume": 0,
        "asr_model": "small",
        "asr_language": "zh",
        "record_max_seconds": 20,
        "silence_stop_ms": 1200
    },
    # Agent 干完活打电话时念的口令。控制台（/console）改的就是这一条。
    "report_line": "高书记，我已经按照你的指示完成了工作。",
    # 打电话开关，控制台顶部那两个勾选框改的就是它。
    #   agent —— say.py / WorkBuddy "干完活汇报"这一路
    #   codex —— codex_notify.py "每轮结束汇报"这一路
    # 关掉之后 /notify 会直接拒绝，电话根本不会响：
    # 闸门放在 bridge 这一层，是因为**所有**路径最后都要走它，
    # 所以关就是真关，不依赖调用方自觉。
    "call_switch": {
        "agent": True,
        "codex": True
    }
}


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = path or os.path.join(HERE, "config.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            user_cfg = json.load(fh)
        for key, value in user_cfg.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


def parse_tag(header_value):
    match = re.search(r";tag=([^;\s>]+)", header_value or "", re.I)
    return match.group(1) if match else None


def with_to_tag(headers, tag):
    out = []
    for name, value in headers:
        if name.lower() in ("to", "t") and "tag=" not in value.lower():
            out.append((name, value + ";tag=" + tag))
        else:
            out.append((name, value))
    return out


class Registration(object):
    def __init__(self, user, host, port, contact, expires, user_agent):
        self.user = user
        self.host = host
        self.port = port
        self.contact = contact
        self.expires = expires
        self.user_agent = user_agent
        self.updated_at = time.time()

    def expired(self):
        return time.time() > self.updated_at + self.expires

    def as_dict(self):
        return {
            "user": self.user,
            "host": self.host,
            "port": self.port,
            "contact": self.contact,
            "expires_in": max(0, int(self.updated_at + self.expires - time.time())),
            "user_agent": self.user_agent,
        }


class Call(object):
    def __init__(self, server, direction, call_id, local_tag, remote_tag, peer, local_ip):
        self.server = server
        self.direction = direction
        self.call_id = call_id
        self.local_tag = local_tag
        self.remote_tag = remote_tag
        self.peer_user, self.peer_host, self.peer_port = peer
        self.local_ip = local_ip

        self.agent_user = "6000"
        self.caller_name = "WorkBuddy"
        self.cseq = 1
        # 去电 INVITE 用的 Via branch。发 CANCEL 必须复用同一个 branch（RFC 3261），
        # 否则话机可能不认这条取消，继续响铃。
        self.branch = None

        self.rtp = None
        self.rtp_remote = (None, 0)
        self.sdp_answer = None

        self.state = "initiated"
        self.created_at = time.time()
        self.answered_at = None
        self.ended_at = None
        self.digits = []
        self.transcript = None
        self.last_text = None
        self.error = None
        self.acked = False

    # -- media helpers usable from a handler

    def feed(self, pcm):
        if self.rtp:
            self.rtp.feed_pcm(pcm)

    def say(self, text, voice_name=None):
        pcm = voice.synthesize(text, voice=voice_name or
                               self.server.cfg["voice"].get("tts_voice") or None)
        self.feed(pcm)
        return pcm

    def wait_until_spoken(self, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.rtp is None or self.rtp.queued_frames() == 0:
                return True
            if self.state == "ended":
                return True
            time.sleep(0.05)
        return False

    def hangup(self):
        self.server.hangup(self)

    @property
    def uri(self):
        return "sip:%s@%s:%d" % (self.peer_user, self.peer_host, self.peer_port)

    def as_dict(self):
        return {
            "call_id": self.call_id,
            "direction": self.direction,
            "peer": "sip:%s@%s:%d" % (self.peer_user, self.peer_host, self.peer_port),
            "state": self.state,
            "duration": round((self.ended_at or time.time()) -
                              (self.answered_at or self.created_at), 1),
            "digits": "".join(self.digits),
            "transcript": self.transcript,
            "error": self.error,
            "rtp": {
                "local_port": self.rtp.local_port if self.rtp else None,
                "remote": self.rtp_remote,
                "sent": self.rtp.packets_sent if self.rtp else 0,
                "received": self.rtp.packets_received if self.rtp else 0,
            },
        }


class SIPServer(object):
    def __init__(self, config=None, handler=None, log=None):
        self.cfg = config or load_config()
        self.handler = handler
        self.log = log or (lambda msg: print(msg, flush=True))

        sip_cfg = self.cfg["sip"]
        self.listen_ip = sip_cfg.get("listen", "0.0.0.0")
        self.port = int(sip_cfg.get("port", 5060))
        self.advertise_ip = sip_cfg.get("advertise_ip") or None
        self.codecs = tuple(self.cfg.get("codecs", ["PCMA", "PCMU"]))
        self.rtp_ports = self.cfg.get("rtp_ports") or None
        self.invite_headers = list(self.cfg.get("invite_headers", []))
        self.agent_user = sorted(self.cfg.get("agents", {"6000": "WorkBuddy"}))[0]

        self.registrations = {}
        self.reg_lock = threading.Lock()
        self.calls = {}
        self.call_lock = threading.Lock()

        self.sock = None
        self._stop = threading.Event()
        self.started_at = time.time()

    # ---------------------------------------------------------------- lifecycle

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.sock.bind((self.listen_ip, self.port))
        self.sock.settimeout(0.5)
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._janitor, daemon=True).start()
        self.log("[SIP] listening on udp/%s:%d as agent %s"
                 % (self.listen_ip, self.port, self.agent_user))
        return self

    def stop(self):
        self._stop.set()
        for call in list(self.calls.values()):
            self.hangup(call)
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass

    # ----------------------------------------------------------------- receiving

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self.handle_datagram(data, addr)
            except Exception:
                self.log("[SIP] dispatch error\n" + traceback.format_exc())

    def handle_datagram(self, data, addr):
        start, headers, body = sip.parse_message(data)
        if not start.strip():
            # 话机的 keepalive 是裸 CRLF，不是 SIP 消息。以前这里会回 405 并刷屏
            # "unsupported method: "，纯噪音，直接丢掉。
            return
        if start.upper().startswith("SIP/"):
            self._on_response(start, headers, body, addr)
            return
        method = sip.method_of(start)
        if method == "REGISTER":
            self._on_register(headers, addr)
        elif method == "INVITE":
            self._on_invite(headers, body, addr)
        elif method == "ACK":
            self._on_ack(headers, addr)
        elif method == "BYE":
            self._on_bye(headers, addr)
        elif method == "CANCEL":
            self._on_cancel(headers, addr)
        elif method == "OPTIONS":
            self._reply(headers, addr, 200, "OK", extra=["Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, INFO"])
        elif method == "INFO":
            self._reply(headers, addr, 200, "OK")
        else:
            self.log("[SIP] unsupported method: %s" % method)
            self._reply(headers, addr, 405, "Method Not Allowed")

    def _send(self, payload, addr):
        try:
            self.sock.sendto(payload, addr)
        except OSError as exc:
            self.log("[SIP] send failed: %s" % exc)

    def _reply(self, headers, addr, code, reason, extra=(), body=""):
        raw = sip.build_response("", headers, code, reason, extra=extra, body=body)
        self._send(raw, addr)

    # ---------------------------------------------------------------- registrar

    def _on_register(self, headers, addr):
        to_header = sip.header_get(headers, "to") or ""
        user, _host, _port = sip.parse_uri(to_header)
        if not user:
            self._reply(headers, addr, 400, "Bad Request")
            return

        contact = sip.header_get(headers, "contact") or ""
        c_user, c_host, c_port = sip.parse_uri(contact)

        expires = None
        match = re.search(r"expires\s*=\s*(\d+)", contact, re.I)
        if match:
            expires = int(match.group(1))
        elif sip.header_get(headers, "expires"):
            raw = sip.header_get(headers, "expires").strip()
            if raw.isdigit():
                expires = int(raw)
        if expires is None:
            expires = int(self.cfg.get("registrar_ttl", 3600))

        # 关键：话机会用**我们在 200 OK 里回的有效期**决定下次什么时候重新注册。
        # 要是照它要的 3600 秒回，话机半小时都不会再吭声——这时候你重启一次
        # bridge.py，注册表就空了，得干等半小时。所以这里把granted 的有效期
        # 压到 registrar_ttl（默认 120 秒），话机就会每分钟左右重新注册一次，
        # 重启服务只是掉线几十秒的事。
        try:
            max_ttl = int(self.cfg.get("registrar_ttl", 3600))
        except (TypeError, ValueError):
            max_ttl = 3600
        if max_ttl > 0:
            expires = min(expires, max_ttl)

        if not c_host:
            c_host, c_port = sip.parse_via_sent_by(headers)
        if not c_host:
            c_host, c_port = addr
        if not c_port:
            c_port = 5060
        c_user = c_user or user

        if expires <= 0:
            with self.reg_lock:
                self.registrations.pop(user, None)
            self.log("[SIP] unregister %s" % user)
        else:
            reg = Registration(user, c_host, c_port,
                               "sip:%s@%s:%d" % (c_user, c_host, c_port),
                               expires, sip.header_get(headers, "user-agent"))
            with self.reg_lock:
                self.registrations[user] = reg
            self.log("[SIP] register   %s -> %s:%d  ttl=%ds  ua=%s"
                     % (user, c_host, c_port, expires, reg.user_agent))

        extra = [
            "Contact: <sip:%s@%s:%d>;expires=%d" % (c_user, c_host, c_port, max(expires, 0)),
            "Expires: %d" % max(expires, 0),
        ]
        self._reply(headers, addr, 200, "OK", extra=extra)

    # ------------------------------------------------------------------ inbound

    def _on_invite(self, headers, body, addr):
        call_id = sip.header_get(headers, "call-id") or sip.new_call_id(addr[0])
        from_header = sip.header_get(headers, "from") or ""
        to_header = sip.header_get(headers, "to") or ""
        f_user, f_host, f_port = sip.parse_uri(from_header)
        t_user, _t_host, _t_port = sip.parse_uri(to_header)

        self._reply(headers, addr, 100, "Trying")

        peer_sdp = sip.parse_sdp(body)
        payload_type, law = sip.pick_codec(peer_sdp, self.codecs)
        if payload_type is None:
            self.log("[SIP] inbound INVITE has no usable G.711 codec: %s"
                     % (peer_sdp.get("payloads"),))
            self._reply(headers, addr, 488, "Not Acceptable Here")
            return

        remote_ip = peer_sdp.get("ip") or addr[0]
        remote_port = peer_sdp.get("port") or 0
        local_ip = self.local_ip_for(addr[0])
        local_tag = sip.new_tag()

        call = Call(self, "inbound", call_id, local_tag,
                    parse_tag(from_header),
                    (f_user or "unknown", f_host or addr[0], f_port or addr[1]),
                    local_ip)
        call.peer_host = addr[0]
        call.peer_port = addr[1]
        call.rtp_remote = (remote_ip, remote_port)
        call.rtp = rtp.RTPSession(local_ip, remote_ip, remote_port,
                                  payload_type, law,
                                  on_dtmf=lambda d: self._on_digit(call, d),
                                  log=self.log, port_range=self.rtp_ports)
        self._register_call(call)

        answer_sdp = sip.build_sdp(local_ip, call.rtp.local_port, self.codecs)
        extra = [
            "Contact: <sip:%s@%s:%d>" % (self.agent_user, local_ip, self.port),
            "Content-Type: application/sdp",
        ]
        raw = sip.build_response("", with_to_tag(headers, local_tag),
                                 200, "OK", extra=extra, body=answer_sdp)
        self._send(raw, addr)
        call.state = "answered"
        call.answered_at = time.time()
        call.sdp_answer = answer_sdp

        self.log("[CALL] inbound from %s@s%s:%d" % (call.peer_user, call.peer_host, call.peer_port))
        call.rtp.start()

        if self.handler:
            threading.Thread(target=self._run_handler, args=(call,), daemon=True).start()

    def _run_handler(self, call):
        try:
            self.handler.on_call(call)
        except Exception:
            self.log("[CALL] handler error\n" + traceback.format_exc())
        finally:
            # 只有**来电**才在这里收尾。去电（通知电话）是 originate()/notify() 发起的，
            # 由它们自己播报完再挂断。这里要是也插一脚，电话会在接通的瞬间被切断，
            # 表现就是"话机明明接了，却报 no response within 45s"。
            if call.direction != "outbound" and call.state not in ("ended", "failed"):
                self.hangup(call)

    def _on_ack(self, headers, addr):
        call = self._find_call(headers)
        if call:
            call.acked = True

    def _on_bye(self, headers, addr):
        self._reply(headers, addr, 200, "OK")
        call = self._find_call(headers)
        if call:
            self.log("[CALL] remote BYE (%s)" % call.state)
            self._teardown(call, "ended")

    def _on_cancel(self, headers, addr):
        self._reply(headers, addr, 200, "OK")
        call = self._find_call(headers)
        if call:
            self._teardown(call, "failed")
            call.error = "cancelled"

    def _on_digit(self, call, digit):
        call.digits.append(digit)
        self.log("[CALL] DTMF %s" % digit)
        if self.handler and hasattr(self.handler, "on_dtmf"):
            try:
                self.handler.on_dtmf(call, digit)
            except Exception:
                self.log("[CALL] dtmf handler error\n" + traceback.format_exc())

    # ----------------------------------------------------------------- outbound

    def local_ip_for(self, peer_host):
        if self.advertise_ip:
            return self.advertise_ip
        return sip.local_ip_towards(peer_host, self.port)

    def lookup(self, extension):
        with self.reg_lock:
            reg = self.registrations.get(extension)
        if reg is None or reg.expired():
            return None
        return reg

    def auto_answer_headers(self, ring_seconds=None, mode=None):
        """构造"自动接听"用的 INVITE 头。两种模式各有取舍，实测结论如下。

        mode = "header"（服务端控制）
            在 INVITE 里带自动接听头。
              ring_seconds = 0 -> alert-autoanswer + answer-after=0  （一响就接）
              ring_seconds > 0 -> 只发 Call-Info: ...;answer-after=N
            ⚠️ **这种方式话机不播正常来电铃声。** 实测 T21P E2：
               延迟期间放的是"自动接听提示音"（那种 du du du 的短音，`EnableAutoAnswerTone`）；
               把那声提示音一关，延迟期间就**彻底静音**，干等到接通。
               所以想要"正常铃声"，别用这个模式。

        mode = "phone"（话机端控制）★ 想听正常铃声就用它
            **一个头都不发**，INVITE 就是普通来电，话机正常响铃；
            由话机自己的 AccountAutoAnswerSwitch + AutoAnswerDelay 计时，响够了自动接起。
            代价：话机那两项设置必须生效，被恢复出厂设置就会退化成"一直响没人接"。
        """
        if mode is None:
            mode = str(self.cfg.get("auto_answer_mode", "header")).lower()
        if mode == "phone":
            return []

        if ring_seconds is None:
            try:
                ring_seconds = int(self.cfg.get("ring_seconds", 0) or 0)
            except (TypeError, ValueError):
                ring_seconds = 0
        if ring_seconds <= 0:
            return list(self.invite_headers)

        out = []
        for header in self.invite_headers:
            if "answer-after" in header.lower():
                out.append(re.sub(r"answer-after=\d+", "answer-after=%d" % ring_seconds,
                                  header, flags=re.I))
        if not out:
            out.append("Call-Info: <sip:127.0.0.1>;answer-after=%d" % ring_seconds)
        return out

    def _create_outbound_call(self, extension, caller_name, agent_user):
        """建好一条去电的 Call 对象（还没发 INVITE）。"""
        reg = self.lookup(extension)
        if reg is None:
            raise RuntimeError("extension %s is not registered" % extension)

        agent_user = agent_user or self.agent_user
        local_ip = self.local_ip_for(reg.host)
        call = Call(self, "outbound", sip.new_call_id(local_ip), sip.new_tag(), None,
                    (extension, reg.host, reg.port), local_ip)
        call.agent_user = agent_user
        call.caller_name = caller_name
        call.cseq = 1
        call.branch = sip.new_branch()

        call.rtp = rtp.RTPSession(local_ip, reg.host, reg.port, sip.PCMA, "alaw",
                                  on_dtmf=lambda d: self._on_digit(call, d),
                                  log=self.log, port_range=self.rtp_ports)
        return call, reg, local_ip

    def _invite_request(self, call, reg, local_ip, auto_answer=True, answer_after=None):
        """拼一条 INVITE。

        auto_answer=False 时**一个自动接听头都不带** —— 对讲机来说这就是普通来电，
        它会用正常铃声一直响。这正是"想听正常铃声"要的。
        """
        body = sip.build_sdp(local_ip, call.rtp.local_port, self.codecs)
        lines = [
            "Via: " + sip.via_for(local_ip, self.port, call.branch),
            "Max-Forwards: 70",
            'From: "%s" <sip:%s@%s>;tag=%s' % (call.caller_name, call.agent_user,
                                               local_ip, call.local_tag),
            "To: <sip:%s@%s:%d>" % (call.peer_user, call.peer_host, call.peer_port),
            "Call-ID: %s" % call.call_id,
            "CSeq: %d INVITE" % call.cseq,
            "Contact: <sip:%s@%s:%d>" % (call.agent_user, local_ip, self.port),
            "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, INFO",
            "Supported: replaces",
            "Content-Type: application/sdp",
        ]
        if auto_answer:
            lines += self.auto_answer_headers(answer_after)
        return sip.build_request("INVITE", call.uri, lines, body=body)

    def ring_only(self, extension, caller_name="WorkBuddy", agent_user=None):
        """打过去让它**响铃**，但不等接听，立刻返回 Call。

        为什么需要这个：实测 Yealink T21P E2 对"自动接听"的来电**永远不播正常铃声**
        ——要么是自动接听提示音（du du du），要么（把提示音关了）干脆静音。
        想让话机响正常来电铃声，就必须先当一个普通来电打过去；
        响够了再用 hangup() 发 CANCEL 取消，然后重打一次带自动接听头的。
        """
        call, reg, local_ip = self._create_outbound_call(extension, caller_name, agent_user)
        raw = self._invite_request(call, reg, local_ip, auto_answer=False)
        self._register_call(call)
        self.log("[CALL] ring only -> %s (%s:%d)" % (extension, reg.host, reg.port))
        self._send(raw, (reg.host, reg.port))
        return call

    def originate(self, extension, caller_name="WorkBuddy", timeout=15,
                  auto_answer=True, agent_user=None, answer_after=None):
        """Call a registered IP phone. Returns a Call in state 'answered'."""
        call, reg, local_ip = self._create_outbound_call(extension, caller_name, agent_user)
        raw = self._invite_request(call, reg, local_ip, auto_answer=auto_answer,
                                   answer_after=answer_after)
        self._register_call(call)

        self.log("[CALL] outbound -> %s (%s:%d)" % (extension, reg.host, reg.port))
        deadline = time.time() + timeout
        next_send = 0.0
        backoff = 0.5
        while time.time() < deadline:
            if call.state in ("answered", "failed"):
                break
            if call.state == "initiated" and time.time() >= next_send:
                self._send(raw, (reg.host, reg.port))
                next_send = time.time() + backoff
                backoff = min(backoff * 2, 4.0)
            time.sleep(0.05)

        if call.state == "answered":
            return call
        if call.state != "failed":
            call.error = "no response within %ds" % timeout
        self.hangup(call)
        raise RuntimeError("call to %s failed: %s" % (extension, call.error))

    def _on_response(self, start, headers, body, addr):
        code = sip.status_of(start)
        call = self._find_call(headers)
        if not call or call.direction != "outbound":
            return
        cseq = sip.header_get(headers, "cseq") or ""
        if cseq.split()[-1].upper() != "INVITE":
            return

        if code < 200:
            if code >= 180:
                call.state = "ringing"
            return

        if code == 200:
            if call.state == "answered":
                self._send_ack(call, sip.header_get(headers, "to") or "")
                return
            call.remote_tag = parse_tag(sip.header_get(headers, "to"))
            peer_sdp = sip.parse_sdp(body)
            if peer_sdp.get("port"):
                call.rtp_remote = (peer_sdp.get("ip") or addr[0], peer_sdp["port"])
            call.sdp_answer = body
            self._send_ack(call, sip.header_get(headers, "to") or "")
            call.state = "answered"
            call.answered_at = time.time()
            if call.rtp:
                call.rtp.remote_ip, call.rtp.remote_port = call.rtp_remote
                call.rtp.start()
            self.log("[CALL] answered by %s, RTP -> %s:%s"
                     % (call.peer_user, call.rtp_remote[0], call.rtp_remote[1]))
            if self.handler:
                threading.Thread(target=self._run_handler, args=(call,), daemon=True).start()
            return

        if code >= 300:
            call.state = "failed"
            call.error = start.split(" ", 2)[-1].strip() or str(code)
            # 非 2xx 的最终响应也必须回 ACK（复用 INVITE 的 branch），否则对端会不停重传。
            self._send_ack(call, sip.header_get(headers, "to") or "", branch=call.branch)
            if code == 487:
                # 487 = 我们自己的 CANCEL 生效了。两段式响铃的正常结果，不是错误。
                # 对端可能重传几份同样的 487，只报第一次，免得日志被刷满。
                if not getattr(call, "_cancel_said", False):
                    self.log("[CALL] ringing cancelled (487)")
                    call._cancel_said = True
            else:
                self.log("[CALL] rejected: %d %s" % (code, call.error))

    def _send_ack(self, call, to_header, branch=None):
        """回 ACK。

        branch 的讲究（RFC 3261 §17.1.1.3）：**非 2xx** 的 ACK 属于 INVITE 事务本身，
        必须复用 INVITE 的同一个 branch；**2xx** 的 ACK 才是新事务、用新 branch。
        搞错了对端会认为没收到 ACK，把 487 一直重传下去（日志里刷屏）。
        """
        lines = [
            "Via: " + sip.via_for(call.local_ip, self.port, branch or sip.new_branch()),
            "Max-Forwards: 70",
            'From: "%s" <sip:%s@%s>;tag=%s' % (call.caller_name, call.agent_user,
                                               call.local_ip, call.local_tag),
            "To: %s" % to_header,
            "Call-ID: %s" % call.call_id,
            "CSeq: %d ACK" % call.cseq,
            "Contact: <sip:%s@%s:%d>" % (call.agent_user, call.local_ip, self.port),
        ]
        raw = sip.build_request("ACK", call.uri, lines, body="")
        self._send(raw, (call.peer_host, call.peer_port))

    # ------------------------------------------------------------------ teardown

    def hangup(self, call):
        if call.state in ("ended", "failed"):
            return
        # 通话只要已建立就必须发 BYE —— 注意这里**不能只认 "answered"**：
        # 播报前状态会被置成 "speaking"，原来那个判断不成立，
        # 结果收尾挂断只清了本地记录、没往话机发 BYE，
        # 话机那头以为还通着，用户只能自己去挂断（实测踩到过）。
        if call.direction == "outbound" and call.state not in ("initiated", "ringing"):
            lines = [
                "Via: " + sip.via_for(call.local_ip, self.port, call.branch or sip.new_branch()),
                "Max-Forwards: 70",
                'From: "%s" <sip:%s@%s>;tag=%s' % (call.caller_name, call.agent_user,
                                                   call.local_ip, call.local_tag),
                "To: <sip:%s@%s:%d>%s" % (call.peer_user, call.peer_host, call.peer_port,
                                          ";tag=" + call.remote_tag if call.remote_tag else ""),
                "Call-ID: %s" % call.call_id,
                "CSeq: %d BYE" % (call.cseq + 1),
                "Contact: <sip:%s@%s:%d>" % (call.agent_user, call.local_ip, self.port),
            ]
            raw = sip.build_request("BYE", call.uri, lines, body="")
            self._send(raw, (call.peer_host, call.peer_port))
            # 明确记一行：这一行出现，才说明是**我们**挂的电话。
            # 没有它只有 [RTP] ended，就说明之前那个 bug 又回来了——
            # 话机收不到 BYE，会一直以为还通着，只能人工挂断。
            self.log("[CALL] sent BYE to %s (we hung up first)" % call.peer_user)
        elif call.direction == "outbound" and call.state in ("initiated", "ringing"):
            # 还没接通就放弃 —— 必须发 CANCEL，否则话机会一直响下去。
            # CANCEL 是"取消 INVITE 事务"，所以：CSeq 号和 INVITE 相同、分支相同。
            lines = [
                "Via: " + sip.via_for(call.local_ip, self.port, call.branch or sip.new_branch()),
                "Max-Forwards: 70",
                'From: "%s" <sip:%s@%s>;tag=%s' % (call.caller_name, call.agent_user,
                                                   call.local_ip, call.local_tag),
                "To: <sip:%s@%s:%d>" % (call.peer_user, call.peer_host, call.peer_port),
                "Call-ID: %s" % call.call_id,
                "CSeq: %d CANCEL" % call.cseq,
            ]
            raw = sip.build_request("CANCEL", call.uri, lines, body="")
            self._send(raw, (call.peer_host, call.peer_port))
            self.log("[CALL] cancelled ringing call to %s" % call.peer_user)
        self._teardown(call, "ended")

    def _teardown(self, call, state):
        call.state = state
        call.ended_at = time.time()
        if call.rtp:
            # 通话结束时报一下实际收发了多少音频包。播放没声音的时候，
            # 一眼就能看出是"包根本没发出去"还是"发出去了但话机没响"。
            sent = getattr(call.rtp, "packets_sent", 0)
            received = getattr(call.rtp, "packets_received", 0)
            self.log("[RTP] %s call ended (%s)  sent=%d  received=%d"
                     % (call.direction, state, sent, received))
            call.rtp.stop()

    # ------------------------------------------------------------------ registry

    def _register_call(self, call):
        with self.call_lock:
            self.calls[call.call_id] = call

    def _find_call(self, headers):
        call_id = sip.header_get(headers, "call-id")
        with self.call_lock:
            return self.calls.get(call_id)

    def _janitor(self):
        while not self._stop.is_set():
            time.sleep(5)
            now = time.time()
            with self.reg_lock:
                for user in [u for u, r in self.registrations.items() if r.expired()]:
                    self.registrations.pop(user, None)
                    self.log("[SIP] registration expired: %s" % user)

    # -------------------------------------------------------------------- status

    def status(self):
        with self.reg_lock:
            regs = {u: r.as_dict() for u, r in self.registrations.items()}
        with self.call_lock:
            calls = [c.as_dict() for c in self.calls.values()]
        return {
            "uptime": round(time.time() - self.started_at, 1),
            "agent": self.agent_user,
            "uplink": self.cfg.get("uplink", "asr"),
            "registrations": regs,
            "calls": calls[-10:],
            "asr_backend": voice.asr_backend_name(),
            # 排查"为什么电话不响"时第一眼要看的东西
            "call_switch": {
                "agent": bool((self.cfg.get("call_switch") or {}).get("agent", True)),
                "codex": bool((self.cfg.get("call_switch") or {}).get("codex", True)),
            },
        }


# ------------------------------------------------------------------ HTTP control

def build_http_handler(server):
    class ControlHandler(BaseHTTPRequestHandler):
        server_version = "VibePhonePBX/0.1"

        def log_message(self, fmt, *args):
            return

        def _send_json(self, obj, code=200):
            payload = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_bytes(self, payload, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_html(self, html):
            self._send_bytes(html.encode("utf-8"), "text/html; charset=utf-8")

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                return {}

        def _send_preview_wav(self):
            """把试听音频发给浏览器。

            只认 PREVIEW_DIR 下的**纯文件名** —— 请求里带 ../ 之类一律拒掉，
            免得这个口变成任意文件读取。
            """
            name = os.path.basename(parse_qs(urlparse(self.path).query)
                                    .get("f", [""])[0])
            path = os.path.join(PREVIEW_DIR, name)
            if not name or not os.path.isfile(path):
                self._send_json({"error": "not found"}, 404)
                return
            with open(path, "rb") as fh:
                self._send_bytes(fh.read(), "audio/wav")

        def do_GET(self):
            if self.path.startswith("/status"):
                self._send_json(server.status())
            elif self.path.startswith("/console"):
                self._send_html(console.PAGE)
            elif self.path.startswith("/api/console"):
                self._send_json(console_state(server))
            elif self.path.startswith("/api/settings"):
                self._send_json(console_state(server))
            elif self.path.startswith("/preview.wav"):
                self._send_preview_wav()
            elif self.path.startswith("/voices"):
                # 老接口，保持原样，别把已有的调用方弄坏
                self._send_json({"voices": voice.list_voices(),
                                 "edge_voices": [v["name"] for v in voice.edge_voices()]})
            else:
                self._send_json({"error": "not found"}, 404)

        def do_POST(self):
            data = self._read_json()
            try:
                if self.path.startswith("/notify"):
                    self._send_json(notify(server, data,
                                           wait=bool(data.get("wait", True))))
                elif self.path.startswith("/api/preview"):
                    self._send_json(make_preview(server, data))
                elif self.path.startswith("/api/save"):
                    self._send_json(save_voice_settings(server, data))
                elif self.path.startswith("/api/switch"):
                    self._send_json(save_call_switch(server, data))
                elif self.path.startswith("/call"):
                    self._send_json(place_call(server, data))
                else:
                    self._send_json({"error": "not found"}, 404)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 500)

    return ControlHandler


def voice_args(server, data):
    """从一次请求里取出 TTS 设置，没给的用 config.json 里的，再没给就用模块默认。

    为什么要在请求里也能带：控制台里刚拖完滑块、还没点"保存"的时候，
    "拨一通"也得按你眼前看到的设置出声——不然试听和真拨出去的声音对不上。
    """
    vcfg = server.cfg.get("voice") or {}

    def pick(key, cast=None):
        value = data.get(key)
        if value is None or value == "":
            value = vcfg.get(key)
        if value is None or value == "":
            return None
        if cast is None:
            return value
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    return {
        "backend": pick("backend"),
        "voice": pick("voice") or pick("tts_voice"),
        "rate": pick("rate", int),
        "pitch": pick("pitch", int),
        "volume": pick("volume", int),
    }


def console_state(server):
    """开控制台页面时要的全部东西：当前设置 + 两套音色清单。"""
    vcfg = server.cfg.get("voice") or {}
    edge = voice.edge_voices()
    return {
        "backend": str(vcfg.get("backend") or "sapi"),
        "voice": vcfg.get("tts_voice") or "",
        "rate": int(vcfg.get("rate") or 0),
        "pitch": int(vcfg.get("pitch") or 0),
        "volume": int(vcfg.get("volume") or 0),
        "report_line": server.cfg.get("report_line") or "",
        "call_switch": {
            "agent": bool((server.cfg.get("call_switch") or {}).get("agent", True)),
            "codex": bool((server.cfg.get("call_switch") or {}).get("codex", True)),
        },
        "default_extension": str(server.cfg.get("default_extension") or ""),
        "sapi_voices": voice.list_voices(),
        # edge 音色只挑控制台要用的字段，别把整个 VoiceInfo 塞给浏览器
        "edge_voices": [{"name": v["name"], "gender": v["gender"],
                         "locale": v["locale"]} for v in edge],
    }


def make_preview(server, data):
    """合成一段"控制台里当前设置"的音频，返回浏览器能直接播的地址。

    注意合成的是 **8 kHz**，也就是话机实际会播的东西 ——
    试听如果用好音质，就会出现"试听很棒、打过去不对"的落差。
    """
    text = (data.get("text") or "").strip()
    if not text:
        raise ValueError("'text' is required")
    pcm = voice.synthesize(text, **voice_args(server, data))
    if not pcm:
        raise ValueError("合成结果为空")

    os.makedirs(PREVIEW_DIR, exist_ok=True)
    old = sorted(glob.glob(os.path.join(PREVIEW_DIR, "preview-*.wav")))
    for path in old[:-PREVIEW_KEEP]:
        try:
            os.remove(path)
        except OSError:
            pass
    name = "preview-%d.wav" % int(time.time() * 1000)
    rtp.write_wav_pcm(os.path.join(PREVIEW_DIR, name), pcm)
    return {
        "ok": True,
        "url": "/preview.wav?f=" + name,
        "seconds": round(len(pcm) / 2.0 / rtp.SAMPLE_RATE, 2),
    }


def save_voice_settings(server, data):
    """把控制台的设置写回 config.json，并在当前进程**立刻生效**。

    两件事都得做，因为读设置的是两拨人：
      · say.py / codex_notify.py 各自去读 config.json
      · 正在跑的 bridge 读的是内存里的 server.cfg
    只写盘不更新内存，症状就是"点了保存，下一通还是老声音"。
    """
    path = os.path.join(HERE, "config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}

    vcfg = cfg.setdefault("voice", {})
    backend = str(data.get("backend") or "").lower()
    if backend in ("sapi", "edge"):
        vcfg["backend"] = backend
    if data.get("voice") is not None:
        vcfg["tts_voice"] = str(data.get("voice") or "")
    for key in ("rate", "pitch", "volume"):
        if data.get(key) is not None:
            try:
                vcfg[key] = int(data[key])
            except (TypeError, ValueError):
                pass
    if data.get("report_line") is not None:
        line = str(data.get("report_line")).strip()
        if line:
            cfg["report_line"] = line

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    server.cfg.setdefault("voice", {}).update(vcfg)
    if cfg.get("report_line"):
        server.cfg["report_line"] = cfg["report_line"]
    applied = voice.configure(server.cfg["voice"])
    log = getattr(server, "log", None)
    if log:
        log("[CONSOLE] 设置已保存：backend=%s voice=%r 语速%+d 音调%+d 音量%+d"
            % (applied.get("backend"), applied.get("voice"),
               int(applied.get("rate") or 0), int(applied.get("pitch") or 0),
               int(applied.get("volume") or 0)))
    return {"ok": True, "applied": applied}


def save_call_switch(server, data):
    """只改那两个打电话开关，写盘 + 当前进程立刻生效。

    单独开一个接口、而不是并进 `/api/save`，是因为开关要**点一下立刻生效**：
    开关这种东西天然是"当下就要起作用"的，攒着等用户想起来点"保存"，
    结果就是他以为关掉了、电话还是响了。
    """
    path = os.path.join(HERE, "config.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}

    switch = cfg.setdefault("call_switch", {})
    for key in ("agent", "codex"):
        if data.get(key) is not None:
            switch[key] = bool(data[key])

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    server.cfg.setdefault("call_switch", {}).update(switch)
    applied = {"agent": bool(switch.get("agent", True)),
               "codex": bool(switch.get("codex", True))}
    log = getattr(server, "log", None)
    if log:
        log("[CONSOLE] 打电话开关：Agent=%s  Codex=%s"
            % ("开" if applied["agent"] else "关",
               "开" if applied["codex"] else "关"))
    return {"ok": True, "call_switch": applied}


def call_allowed(server, data):
    """这一通电话允许打吗？返回 `(允许?, 拒绝原因)`。

    请求里的 `source` 说明是谁在要求打电话：

      · `codex`  —— Codex 的 notify 钩子（每轮结束自动触发）
      · `agent`  —— say.py / 任何"干完活汇报"的脚本（缺省值）
      · `manual` —— 人在控制台里点的"立即拨一通"

    `manual` **不受开关限制**：你人坐在那儿按下按钮，本身就是一次明确意图，
    再被自己设的开关挡回去只会让人以为是坏了。真拨不出去的时候，
    这个按钮也是唯一能确认"是开关问题还是链路问题"的手段。
    """
    source = str(data.get("source") or "agent").strip().lower()
    if source == "manual":
        return True, ""

    switch = server.cfg.get("call_switch") or {}
    key = "codex" if source == "codex" else "agent"
    if switch.get(key, True):
        return True, ""

    who = "Codex 汇报电话" if key == "codex" else "Agent 汇报电话"
    return False, ("%s已关闭（在 http://127.0.0.1:8080/console 顶部可以打开）" % who)


def notify(server, data, wait=True):
    """Ring a phone and speak a piece of text, then hang up."""
    allowed, reason = call_allowed(server, data)
    if not allowed:
        server.log("[CALL] 没打：%s（来源 %s）"
                   % (reason, data.get("source") or "agent"))
        return {"ok": False, "skipped": True, "reason": reason}

    text = (data.get("text") or "").strip()
    if not text:
        raise ValueError("'text' is required")
    extension = str(data.get("extension") or server.cfg["default_extension"])
    caller = data.get("caller") or "WorkBuddy"
    # 15 秒对"人要跑过去接电话"来说太短了：超时后 originate 会直接挂断，
    # 结果话机明明接起来了却报 no response。默认放到 45 秒。
    try:
        answer_timeout = int(data.get("timeout")
                             or server.cfg.get("answer_timeout", 45))
    except (TypeError, ValueError):
        answer_timeout = 45

    # 先响几声再接，有几种实现方式，实测结论写在 auto_answer_headers() 里。
    # 想让话机响**正常来电铃声**，只有 two_stage 这一条路，因为话机对
    # "自动接听"的来电根本不播铃声（要么提示音，要么静音）。
    ring = data.get("ring")
    if ring is None:
        ring = server.cfg.get("ring_seconds", 0)
    try:
        ring = int(ring or 0)
    except (TypeError, ValueError):
        ring = 0
    mode = str(server.cfg.get("auto_answer_mode", "header")).lower()

    def acquire():
        """拿到一条"已接通"的 Call。"""
        if mode == "manual":
            return server.originate(extension, caller_name=caller, timeout=answer_timeout, auto_answer=False)
        if mode != "two_stage" or ring <= 0:
            return server.originate(extension, caller_name=caller, timeout=answer_timeout,
                                    answer_after=(ring if mode == "header" else None))

        # ① 先当普通来电打过去 —— 话机响的是正常铃声
        ringing = server.ring_only(extension, caller_name=caller)
        deadline = time.time() + ring
        while time.time() < deadline:
            if ringing.state == "answered":
                server.log("[CALL] 有人在响铃期间接了，直接用这一通")
                return ringing
            time.sleep(0.05)
        # ② 响够了，取消这一通（否则话机会一直响），
        #    ③ 立刻用"自动接听"重打一次，话机秒接，然后开始说话
        server.hangup(ringing)
        return server.originate(extension, caller_name=caller, timeout=answer_timeout,
                                answer_after=0)

    def job():
        # **先把话合成好，再接通**。
        # SAPI 首次念一句要现合成（起 PowerShell + 合成，两三秒），只有合成过的
        # 句子才会走 cache/ 里的缓存。原来是在接通之后才合成，结果是：对方拿起
        # 听筒先听到一段死寂，很自然就以为没通、挂了。实测有一通就这么被挂掉。
        # 现在让它和"响铃"并行跑——4 秒的铃响刚好把合成的时间盖住，一接通就有声。
        box = {}

        def synth():
            try:
                box["pcm"] = voice.synthesize(text, **voice_args(server, data))
            except Exception:
                box["error"] = traceback.format_exc()

        worker = threading.Thread(target=synth, daemon=True)
        worker.start()
        synth_t0 = time.time()

        call = acquire()
        answered_at = time.time()
        try:
            call.state = "speaking"
            call.last_text = text
            worker.join(timeout=30)
            if "pcm" not in box:
                server.log("[TTS] 合成失败，这一通没话可说：%s"
                           % (box.get("error") or "超时 30 秒"))
                raise RuntimeError("语音合成失败或超时")
            pcm = box["pcm"]
            # 这两行是排障用的：通话时长对不上时，一眼能看出时间花在哪。
            #   ready_at - answered_at = 接通后还静了多久（要趋近 0）
            #   feed_at  - ready_at    = 把音频灌进发送队列花了多久（要吃满 0）
            server.log("[TTS] 音频 %.2fs  合成耗时 %.2fs  接通后等待 %.2fs"
                       % (len(pcm) / 2.0 / 8000.0, answered_at - synth_t0,
                          time.time() - answered_at))
            feed_at = time.time()
            call.feed(pcm)
            server.log("[TTS] 入队 %.3fs，开始播报" % (time.time() - feed_at))
            call.wait_until_spoken()
            if server.handler and hasattr(server.handler, "after_speak"):
                server.handler.after_speak(call)
            time.sleep(0.3)
        finally:
            call.hangup()

    if not wait:
        threading.Thread(target=job, daemon=True).start()
        return {"ok": True, "queued": True, "extension": extension}

    job()
    return {"ok": True, "extension": extension, "text": text}


def place_call(server, data):
    extension = str(data.get("extension") or server.cfg["default_extension"])
    call = server.originate(extension, caller_name=data.get("caller") or "WorkBuddy")
    if call.transcript is not None:
        call.hangup()
    return {"ok": True, "call": call.as_dict()}


def start_http(server):
    http_cfg = server.cfg["http"]
    handler = build_http_handler(server)
    httpd = ThreadingHTTPServer((http_cfg.get("listen", "127.0.0.1"),
                                 int(http_cfg.get("port", 8080))), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    server.log("[HTTP] control API on http://%s:%d/"
               % (http_cfg.get("listen"), http_cfg.get("port")))
    return httpd


if __name__ == "__main__":
    pbx = SIPServer()
    pbx.start()
    start_http(pbx)
    print("SIP registrar + HTTP control API running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pbx.stop()
