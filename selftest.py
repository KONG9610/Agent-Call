"""selftest.py - verify the PBX without any hardware, using a fake IP phone.

Two scenarios are exercised over real UDP sockets on loopback:
  1. the fake phone registers, then the PBX calls it and streams audio
  2. the fake phone calls the PBX, sends audio and a DTMF digit, then hangs up

Run:  python selftest.py
"""

import socket
import struct
import sys
import threading
import time

import g711
import pbx
import sipcore as sip

SIP_PORT = 15060
PHONE_SIP = ("127.0.0.1", 15070)
PHONE_RTP = 15080

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  [%s] %s%s" % ("ok" if ok else "FAIL", name,
                           ("  <- " + str(detail)) if detail and not ok else ""))


def recv_sip(sock, timeout=5.0):
    sock.settimeout(timeout)
    try:
        data, addr = sock.recvfrom(65535)
    except socket.timeout:
        return None, None
    start, headers, body = sip.parse_message(data)
    return (start, headers, body), addr


class FakePhone(object):
    """Speaks just enough SIP to look like a Yealink/Fanvil endpoint."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(PHONE_SIP)
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("127.0.0.1", PHONE_RTP))
        self.rtp.settimeout(0.5)
        self.server_addr = ("127.0.0.1", SIP_PORT)
        self.invite_headers = None
        self.invite_body = None

    def register(self, user="1001", expires=3600):
        lines = [
            "Via: " + sip.via_for(PHONE_SIP[0], PHONE_SIP[1], sip.new_branch()),
            "Max-Forwards: 70",
            "From: <sip:%s@%s>;tag=%s" % (user, PHONE_SIP[0], sip.new_tag()),
            "To: <sip:%s@%s>" % (user, PHONE_SIP[0]),
            "Call-ID: %s" % sip.new_call_id(PHONE_SIP[0]),
            "CSeq: 1 REGISTER",
            "Contact: <sip:%s@%s:%d>;expires=%d" % (user, PHONE_SIP[0], PHONE_SIP[1], expires),
            "User-Agent: FakePhone/1.0",
        ]
        raw = sip.build_request("REGISTER", "sip:%s@%s" % (user, PHONE_SIP[0]), lines)
        self.sock.sendto(raw, self.server_addr)
        return recv_sip(self.sock)

    def wait_invite(self, timeout=5.0):
        (start, headers, body), addr = recv_sip(self.sock, timeout)
        if not start:
            return None
        self.invite_headers = headers
        self.invite_body = body
        return {"start": start, "headers": headers, "body": body, "addr": addr}

    def answer_invite(self, invite, tag=None, rtp_port=PHONE_RTP):
        tag = tag or sip.new_tag()
        headers = invite["headers"]
        sdp = sip.build_sdp("127.0.0.1", rtp_port, ("PCMA", "PCMU"))
        extra = [
            "Contact: <sip:1001@%s:%d>" % (PHONE_SIP[0], PHONE_SIP[1]),
            "Content-Type: application/sdp",
        ]
        raw = sip.build_response("", pbx.with_to_tag(headers, tag), 200, "OK",
                                 extra=extra, body=sdp)
        self.sock.sendto(raw, invite["addr"])
        return tag

    def call_pbx(self, target="6000"):
        self.call_id = sip.new_call_id("127.0.0.1")
        self.local_tag = sip.new_tag()
        lines = [
            "Via: " + sip.via_for(PHONE_SIP[0], PHONE_SIP[1], sip.new_branch()),
            "Max-Forwards: 70",
            "From: <sip:1001@%s>;tag=%s" % (PHONE_SIP[0], self.local_tag),
            "To: <sip:%s@127.0.0.1>" % target,
            "Call-ID: %s" % self.call_id,
            "CSeq: 1 INVITE",
            "Contact: <sip:1001@%s:%d>" % (PHONE_SIP[0], PHONE_SIP[1]),
            "Content-Type: application/sdp",
        ]
        sdp = sip.build_sdp("127.0.0.1", PHONE_RTP, ("PCMA", "PCMU"))
        raw = sip.build_request("INVITE", "sip:%s@127.0.0.1:%d" % (target, SIP_PORT),
                                lines, body=sdp)
        self.sock.sendto(raw, self.server_addr)

    def send_rtp(self, payload, payload_type=8, samples=160):
        header = struct.pack("!BBHII", 0x80, payload_type, self._seq(),
                             self._ts(samples), 0x12345678)
        self.rtp.sendto(header + payload, ("127.0.0.1", self.pbx_rtp_port))

    def send_dtmf(self, digit):
        event = int(digit) if digit.isdigit() else (10 if digit == "*" else 11)
        self.send_rtp(bytes([event, 0x80, 0x0A, 0x00]), payload_type=101, samples=0)

    def _seq(self):
        self._seqno = getattr(self, "_seqno", 0) + 1
        return self._seqno & 0xFFFF

    def _ts(self, samples):
        self._tstamp = getattr(self, "_tstamp", 0) + samples
        return self._tstamp & 0xFFFFFFFF


class StubHandler(object):
    def __init__(self):
        self.calls = []
        self.digits = []

    def on_call(self, call):
        self.calls.append(call)
        call.rtp.start_recording()
        deadline = time.time() + 4
        while time.time() < deadline and call.state not in ("ended", "failed"):
            time.sleep(0.05)
        call.recorded = call.rtp.stop_recording()

    def on_dtmf(self, call, digit):
        self.digits.append(digit)


def test_g711():
    print("\nG.711 codec")
    tone = b"".join(struct.pack("<h", int(8000 * (i % 40 - 20) / 20))
                    for i in range(800))
    for law in (g711.ULAW, g711.ALAW):
        enc = g711.encode(tone, law)
        dec = g711.decode(enc, law)
        check("round trip keeps length (%s)" % law, len(dec) == len(tone))
        src = struct.unpack("<800h", tone)
        dst = struct.unpack("<800h", dec)
        err = max(abs(a - b) for a, b in zip(src, dst))
        check("round trip error stays small (%s)" % law, err < 400, "max err %d" % err)
    check("resample 16k -> 8k halves the samples",
          len(g711.resample(b"\x00\x00" * 1600, 16000, 8000)) == 1600)


def test_registrar(phone):
    print("\nScenario 1: REGISTER")
    reply, _addr = phone.register()
    check("register gets a reply", reply is not None)
    if reply:
        check("register answered 200 OK", sip.status_of(reply[0]) == 200, reply[0])
        check("register reply carries Contact", sip.header_get(reply[1], "contact") is not None)


def test_outbound(server, phone, handler):
    print("\nScenario 2: PBX calls the phone and speaks")
    result = {}

    def worker():
        try:
            result["call"] = server.originate("1001", timeout=8)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    invite = phone.wait_invite(6)
    check("phone received INVITE", invite is not None)
    if not invite:
        thread.join(2)
        return
    check("INVITE is addressed to the phone",
          "INVITE" in invite["start"] and "1001" in invite["start"], invite["start"])

    # INVITE 里带不带"自动接听"头，取决于 config.json 的 auto_answer_mode：
    #   "phone"  -> **一个头都不带**，让它就是普通来电，话机正常响铃，
    #               由话机自己的 AccountAutoAnswerSwitch + AutoAnswerDelay 计时接起。
    #   "header" -> 服务端控制：
    #               ring_seconds=0 -> Alert-Info: alert-autoanswer（一响就接）
    #               ring_seconds>0 -> Call-Info: answer-after=N
    # 所以这里必须按模式分别断言，不能一律要求有 Alert-Info。
    alert_info = (sip.header_get(invite["headers"], "alert-info") or "")
    call_info = (sip.header_get(invite["headers"], "call-info") or "")
    try:
        ring = int(server.cfg.get("ring_seconds", 0) or 0)
    except (TypeError, ValueError):
        ring = 0
    mode = str(server.cfg.get("auto_answer_mode", "header")).lower()

    if mode == "phone":
        check("INVITE is a plain call (no auto-answer headers) so the phone rings normally",
              "answer-after" not in call_info.lower()
              and "alert-autoanswer" not in alert_info.lower(),
              "%s | %s" % (alert_info, call_info))
    elif ring > 0:
        check("INVITE asks the phone to ring %ds before auto-answering" % ring,
              "answer-after=%d" % ring in call_info.lower())
        check("INVITE does NOT carry alert-autoanswer when ringing first",
              "alert-autoanswer" not in alert_info.lower(), alert_info)
    else:
        check("INVITE carries auto-answer (Alert-Info or answer-after=0)",
              "alert-autoanswer" in alert_info.lower()
              or "answer-after=0" in call_info.lower(),
              "%s | %s" % (alert_info, call_info))
    check("INVITE offers G.711 (PCMA or PCMU)",
          "RTP/AVP" in invite["body"], invite["body"][:60])

    phone.answer_invite(invite)
    thread.join(6)
    call = result.get("call")
    check("originate returned an answered call",
          call is not None and call.state == "answered",
          result.get("error") or (call.state if call else None))
    if call is None:
        return

    ack, _addr = recv_sip(phone.sock, 3)
    check("ACK received after 200 OK", ack is not None and "ACK" in ack[0],
          ack[0] if ack else None)

    phone.rtp.settimeout(3.0)
    phone_sdp = sip.parse_sdp(invite["body"])
    call.feed(b"\x40\x1f" * 1600)          # 200 ms of tone
    packets = []
    deadline = time.time() + 3
    while time.time() < deadline and len(packets) < 5:
        try:
            data, _ = phone.rtp.recvfrom(2048)
        except socket.timeout:
            break
        packets.append(data)
    check("phone received RTP audio packets", len(packets) >= 5, len(packets))
    if packets:
        pt = packets[0][1] & 0x7F
        check("RTP payload type is a G.711 one", pt in (0, 8), pt)
        check("RTP packet carries 160 samples", len(packets[0]) - 12 == 160,
              len(packets[0]) - 12)

    call.hangup()
    bye, _addr = recv_sip(phone.sock, 3)
    check("BYE received on hangup", bye is not None and "BYE" in bye[0],
          bye[0] if bye else None)


def test_inbound(server, phone, handler):
    print("\nScenario 3: the phone calls the PBX and presses a key")
    handler.digits.clear()
    phone.call_pbx("6000")

    seen_100 = False
    answer = None
    deadline = time.time() + 6
    while time.time() < deadline and answer is None:
        reply, _addr = recv_sip(phone.sock, 2)
        if reply is None:
            break
        code = sip.status_of(reply[0])
        if code == 100:
            seen_100 = True
        elif code == 200:
            answer = reply
    check("PBX sent 100 Trying", seen_100)
    check("PBX answered 200 OK", answer is not None, answer[0] if answer else None)
    if answer is None:
        return
    check("PBX put a tag on the To header",
          "tag=" in (sip.header_get(answer[1], "to") or ""))

    phone_sdp = sip.parse_sdp(answer[2])
    phone.pbx_rtp_port = phone_sdp.get("port")
    check("PBX offered an RTP port", bool(phone.pbx_rtp_port), phone_sdp)

    ack_lines = [
        "Via: " + sip.via_for(PHONE_SIP[0], PHONE_SIP[1], sip.new_branch()),
        "Max-Forwards: 70",
        "From: <sip:1001@%s>;tag=%s" % (PHONE_SIP[0], phone.local_tag),
        "To: %s" % sip.header_get(answer[1], "to"),
        "Call-ID: %s" % phone.call_id,
        "CSeq: 1 ACK",
        "Contact: <sip:1001@%s:%d>" % (PHONE_SIP[0], PHONE_SIP[1]),
    ]
    phone.sock.sendto(sip.build_request("ACK", "sip:6000@127.0.0.1:%d" % SIP_PORT,
                                        ack_lines), phone.server_addr)

    tone = b"\x00\x40" * 160
    for _ in range(40):
        phone.send_rtp(tone)
        time.sleep(0.02)
    phone.send_dtmf("1")
    time.sleep(0.3)

    check("PBX decoded the DTMF digit", handler.digits == ["1"], handler.digits)
    check("PBX spawned a call handler", len(handler.calls) >= 1)

    bye_lines = [
        "Via: " + sip.via_for(PHONE_SIP[0], PHONE_SIP[1], sip.new_branch()),
        "Max-Forwards: 70",
        "From: <sip:1001@%s>;tag=%s" % (PHONE_SIP[0], phone.local_tag),
        "To: %s" % sip.header_get(answer[1], "to"),
        "Call-ID: %s" % phone.call_id,
        "CSeq: 2 BYE",
    ]
    phone.sock.sendto(sip.build_request("BYE", "sip:6000@127.0.0.1:%d" % SIP_PORT,
                                        bye_lines), phone.server_addr)


def main():
    test_g711()

    config = pbx.load_config()
    config["sip"]["port"] = SIP_PORT
    config["sip"]["listen"] = "127.0.0.1"
    config["sip"]["advertise_ip"] = "127.0.0.1"
    config["http"]["port"] = 18080

    handler = StubHandler()
    server = pbx.SIPServer(config, handler=handler, log=lambda m: print("    " + m))
    server.start()
    phone = FakePhone()

    try:
        test_registrar(phone)
        check("extension 1001 is visible in the registry", "1001" in server.status()["registrations"])
        test_outbound(server, phone, handler)
        test_inbound(server, phone, handler)
    finally:
        server.stop()
        phone.sock.close()
        phone.rtp.close()

    print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
    if FAIL:
        print("failed: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
