"""Minimal SIP (RFC 3261) message parsing / building plus SDP helpers.

Scope is deliberately tiny: only what is needed to register an IP phone,
call it, and exchange RTP audio with it.
"""

import re
import secrets

CRLF = "\r\n"

PCMU = 0
PCMA = 8
TELEPHONE_EVENT = 101

_CODEC_PT = {"PCMU": PCMU, "PCMA": PCMA}
_CODEC_LAW = {PCMU: "ulaw", PCMA: "alaw"}
_CODEC_NAME = {PCMU: "PCMU", PCMA: "PCMA"}

# Compact header forms allowed by RFC 3261 section 7.3.3
_ALIASES = {
    "via": ("via", "v"),
    "from": ("from", "f"),
    "to": ("to", "t"),
    "call-id": ("call-id", "i"),
    "contact": ("contact", "m"),
    "content-type": ("content-type", "c"),
    "content-length": ("content-length", "l"),
    "subject": ("subject", "s"),
    "supported": ("supported", "k"),
    "allow-events": ("allow-events", "u"),
    "content-encoding": ("content-encoding", "e"),
}


def new_branch():
    return "z9hG4bK" + secrets.token_hex(8)


def new_tag():
    return secrets.token_hex(6)


def new_call_id(host):
    return secrets.token_hex(12) + "@" + host


def parse_message(raw):
    """Return (start_line, [(name, value), ...], body)."""
    text = raw.decode("utf-8", "replace")
    head, _, body = text.partition(CRLF + CRLF)
    if not _:
        head, _, body = text.partition("\n\n")
    lines = head.replace("\r\n", "\n").split("\n")
    start = lines[0].strip() if lines else ""
    headers = []
    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers.append((name.strip(), value.strip()))
    return start, headers, body


def header_get(headers, name):
    keys = _ALIASES.get(name.lower(), (name.lower(),))
    for k, v in headers:
        if k.lower() in keys:
            return v
    return None


def header_all(headers, name):
    keys = _ALIASES.get(name.lower(), (name.lower(),))
    return [v for k, v in headers if k.lower() in keys]


def method_of(start_line):
    return start_line.split(" ", 1)[0].upper() if start_line else ""


def status_of(start_line):
    parts = start_line.split(" ", 2)
    if len(parts) >= 2 and parts[0].upper().startswith("SIP/"):
        try:
            return int(parts[1])
        except ValueError:
            return 0
    return 0


def parse_uri(value):
    """Pull (user, host, port) out of a sip:/sips: URI, ignoring parameters."""
    if not value:
        return None, None, None
    m = re.search(r"sips?:([^@\s>;]*)@([^;:>\s]+)(?::(\d+))?", value)
    if not m:
        return None, None, None
    user = m.group(1) or None
    host = m.group(2)
    port = int(m.group(3)) if m.group(3) else 5060
    return user, host, port


def parse_via_sent_by(headers):
    """Best-effort extraction of the (host, port) a request came from."""
    via = header_get(headers, "via")
    if not via:
        return None, None
    m = re.search(r"SIP/2\.0/UDP\s+([^\s;]+)", via)
    if not m:
        return None, None
    hostport = m.group(1)
    if hostport.startswith("["):
        return None, None
    if ":" in hostport:
        host, _, port = hostport.rpartition(":")
        if port.isdigit() and host:
            return host, int(port)
    return hostport, 5060


def via_for(ip, port, branch):
    return "SIP/2.0/UDP %s:%d;branch=%s;rport" % (ip, port, branch)


def echo_core_headers(headers):
    """Via / From / To / Call-ID / CSeq, in that order, as required for responses."""
    out = []
    for name in ("via", "from", "to", "call-id", "cseq"):
        for value in header_all(headers, name):
            out.append("%s: %s" % (_canonical(name), value))
    return out


def _canonical(name):
    return {
        "via": "Via",
        "from": "From",
        "to": "To",
        "call-id": "Call-ID",
        "cseq": "CSeq",
        "contact": "Contact",
        "content-type": "Content-Type",
        "content-length": "Content-Length",
    }.get(name, "-".join(p.capitalize() for p in name.split("-")))


def build_response(start_line, headers, code, reason, extra=(), body=""):
    lines = ["SIP/2.0 %d %s" % (code, reason)]
    lines += echo_core_headers(headers)
    lines += list(extra)
    payload = body.encode("utf-8") if isinstance(body, str) else body
    lines.append("Content-Length: %d" % len(payload))
    text = CRLF.join(lines) + CRLF + CRLF
    return text.encode("utf-8") + payload


def build_request(method, uri, headers, body=""):
    lines = ["%s %s SIP/2.0" % (method, uri)]
    lines += list(headers)
    payload = body.encode("utf-8") if isinstance(body, str) else body
    lines.append("Content-Length: %d" % len(payload))
    text = CRLF.join(lines) + CRLF + CRLF
    return text.encode("utf-8") + payload


def build_sdp(ip, port, codecs=("PCMA", "PCMU")):
    payload_types = []
    rtpmaps = []
    for name in codecs:
        pt = _CODEC_PT[name]
        payload_types.append(str(pt))
        rtpmaps.append("a=rtpmap:%d %s/8000" % (pt, _CODEC_NAME[pt]))
    payload_types.append(str(TELEPHONE_EVENT))
    rtpmaps.append("a=rtpmap:%d telephone-event/8000" % TELEPHONE_EVENT)
    rtpmaps.append("a=fmtp:%d 0-15" % TELEPHONE_EVENT)

    lines = [
        "v=0",
        "o=- %d 1 IN IP4 %s" % (secrets.randbelow(1 << 30), ip),
        "s=VibePhone",
        "c=IN IP4 %s" % ip,
        "t=0 0",
        "m=audio %d RTP/AVP %s" % (port, " ".join(payload_types)),
    ]
    lines += rtpmaps
    lines += ["a=ptime:20", "a=sendrecv"]
    return CRLF.join(lines) + CRLF


def parse_sdp(body):
    """Return a dict describing the peer's media endpoint."""
    info = {"ip": None, "port": None, "payloads": [], "rtpmap": {}, "direction": "sendrecv"}
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("c=IN IP4"):
            parts = line.split()
            if parts:
                info["ip"] = parts[-1]
        elif line.startswith("m=audio"):
            parts = line.split()
            if len(parts) >= 3:
                try:
                    info["port"] = int(parts[1])
                except ValueError:
                    info["port"] = None
                info["payloads"] = [int(p) for p in parts[3:] if p.isdigit()]
        elif line.startswith("a=rtpmap:"):
            rest = line[len("a=rtpmap:"):]
            num, _, value = rest.partition(" ")
            if num.strip().isdigit():
                info["rtpmap"][int(num)] = value.strip()
        elif line.startswith("a=inactive"):
            info["direction"] = "inactive"
        elif line.startswith("a=recvonly"):
            info["direction"] = "recvonly"
        elif line.startswith("a=sendonly"):
            info["direction"] = "sendonly"
    return info


def pick_codec(peer_sdp, preferred=("PCMA", "PCMU")):
    """Choose the first mutually supported G.711 payload type."""
    offered = peer_sdp.get("payloads") or []
    for name in preferred:
        pt = _CODEC_PT[name]
        if pt in offered:
            return pt, _CODEC_LAW[pt]
    for pt in offered:
        if pt in _CODEC_LAW:
            return pt, _CODEC_LAW[pt]
    return None, None


def local_ip_towards(host, port=5060):
    """Ask the OS which local address it would use to reach a peer."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, port))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()
