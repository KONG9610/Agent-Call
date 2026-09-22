"""Yealink 话机 Web 管理接口（Python 客户端）—— 无需浏览器即可读改配置。

话机的网页登录**不是**普通的表单 POST，而是先在浏览器里用 JS 加密。要复刻的流程：

    1. GET  /servlet?p=login&q=getsessionid   -> "<RSA-n>,<RSA-e>,<sessid>"
    2. key = MD5(random) 的 32 位十六进制字符串；iv 同理
    3. key / iv 各自 RSA(PKCS#1 v1.5) 加密后转 hex
    4. 明文 = "MD5=<明文MD5>;rand=..;sessionid=<sessid>;username=..;pwd=..;"
       用 AES-128-CBC(key=key 的 16 字节, iv=iv 的 16 字节, ZeroPadding) 加密 -> base64
    5. POST /servlet?p=login&q=login
       ⚠️ 必须带 Cookie: JSESSIONID=<sessid>，不带就是 404（不是密码错）

登录后，SIP 账号设置是**普通表单 POST**：
    GET  /servlet?p=account-register&q=load&acc=<0|1>      acc 从 0 开始
    POST /servlet?p=account-register&q=write&acc=<0|1>

命令行：
    python yealink_web.py show                       # 列出这台话机的账号配置页字段与当前值
    python yealink_web.py show 0
    python yealink_web.py set 0 1001 192.168.1.10   # 示例电脑地址，必须替换
"""

import base64
import hashlib
import http.client
import random
import re
import sys
import os
from urllib.parse import urlencode

from Crypto.Cipher import AES

DEFAULT_IP = os.environ.get("CODEX_PHONE_IP", "")
ACCOUNT_PAGE = "/servlet?p=account-register&q=load&acc=%d"
ACCOUNT_WRITE = "/servlet?p=account-register&q=write&acc=%d"
ACCOUNT_FIELDS = ("AccountEnable", "AccountLabel", "AccountDisplayName",
                  "AccountRegisterName", "AccountUserName", "AccountPassword",
                  "server1", "port1", "transport1", "expires1", "RetryCounts1",
                  "server2", "port2", "expires2", "RetryCounts2",
                  "OutboundHost1", "OutboundPort1", "OutboundHost2", "OutboundPort2",
                  "AccountOutboundSwitch", "AccountStunSwitch",
                  "AccountProxyFallbackInterval", "var_accountID")


class YealinkError(RuntimeError):
    pass


class YealinkPhone:
    def __init__(self, ip, user="admin", pwd="admin", timeout=8):
        self.ip = ip
        self.user = user
        self.pwd = pwd
        self.timeout = timeout
        self.cookie = None

    # ------------------------------------------------------------------ 传输

    def _request(self, method, path, body=None, cookie=None):
        conn = http.client.HTTPConnection(self.ip, 80, timeout=self.timeout)
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie or self.cookie:
            headers["Cookie"] = cookie or self.cookie
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", "replace")
        result = (resp.status, dict(resp.getheaders()), data)
        conn.close()
        return result

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, fields):
        return self._request("POST", path, urlencode(fields))

    # ------------------------------------------------------------ 登录加密

    @staticmethod
    def _rsa_encrypt(text, n_hex, e_hex):
        """复刻 jsbn 的 RSAKey.encrypt：PKCS#1 v1.5 填充 + 裸模幂，输出偶数长度 hex。"""
        n, e = int(n_hex, 16), int(e_hex, 16)
        size = (n.bit_length() + 7) // 8
        msg = text.encode()
        pad = bytes(random.randint(1, 255) for _ in range(size - len(msg) - 3))
        m = int.from_bytes(b"\x00\x02" + pad + b"\x00" + msg, "big")
        h = format(pow(m, e, n), "x")
        return h if len(h) % 2 == 0 else "0" + h

    @staticmethod
    def _aes_b64(plain, key_hex, iv_hex):
        raw = plain.encode()
        pad = 16 - (len(raw) % 16) or 16          # CryptoJS ZeroPadding
        ct = AES.new(bytes.fromhex(key_hex), AES.MODE_CBC,
                     bytes.fromhex(iv_hex)).encrypt(raw + b"\x00" * pad)
        return base64.b64encode(ct).decode()

    def login(self):
        _, _, body = self.get("/servlet?p=login&q=getsessionid")
        parts = body.strip().split(",")
        if len(parts) != 3:
            raise YealinkError("getsessionid 返回异常: %r" % body[:120])
        n_hex, e_hex, sessid = parts
        key_hex = hashlib.md5(str(random.random()).encode()).hexdigest()
        iv_hex = hashlib.md5(str(random.random()).encode()).hexdigest()
        plain = "rand=%s;sessionid=%s;username=%s;pwd=%s;" % (
            random.random(), sessid, self.user, self.pwd)
        plain = "MD5=%s;%s" % (hashlib.md5(plain.encode()).hexdigest(), plain)
        status, headers, _ = self._request(
            "POST", "/servlet?p=login&q=login",
            urlencode({
                "key": self._rsa_encrypt(key_hex, n_hex, e_hex),
                "iv": self._rsa_encrypt(iv_hex, n_hex, e_hex),
                "data": self._aes_b64(plain, key_hex, iv_hex),
                "jumpto": "status",
                "acc": "",
            }),
            cookie="JSESSIONID=" + sessid)
        if status not in (301, 302, 303):
            raise YealinkError("登录失败（status=%s）：多半是 Web 密码不对，"
                               "或者账号不是 admin" % status)
        self.cookie = "JSESSIONID=" + sessid
        return headers.get("Location")

    # ------------------------------------------------------------ 账号配置

    @staticmethod
    def parse_account_form(html):
        """从账号页里抠出 formInput 的全部字段（含 select 的选中值）。"""
        m = re.search(r'<form[^>]*name="formInput".*?</form>', html, re.S | re.I)
        if not m:
            raise YealinkError("页面上没找到 formInput")
        form = m.group(0)
        fields = {}
        for im in re.finditer(r"<input[^>]*>", form, re.I):
            tag = im.group(0)
            nm = re.search(r'name="([^"]+)"', tag)
            if not nm:
                continue
            name = nm.group(1)
            if re.search(r'type="(button|submit|reset)"', tag, re.I):
                continue
            val = re.search(r'value="([^"]*)"', tag)
            fields[name] = val.group(1) if val else ""
        for sm in re.finditer(r"<select[^>]*name=\"([^\"]+)\"[^>]*>(.*?)</select>", form, re.S | re.I):
            name, body = sm.group(1), sm.group(2)
            value = ""
            for om in re.finditer(r"<option[^>]*>", body, re.I):
                tag = om.group(0)
                if "selected" in tag.lower():          # value 和 selected 的先后顺序不固定
                    v = re.search(r'value="([^"]*)"', tag)
                    value = v.group(1) if v else ""
                    break
            fields[name] = value
        return fields

    @staticmethod
    def page_token(html):
        """页面上的 g_strToken。话机的 JS 会用 UpdateToken() 把它塞进每个 form 的
        隐藏域 token——不带这个字段提交，服务器直接回 403 User Identity Forbidden。"""
        m = re.search(r'g_strToken\s*=\s*"([^"]*)"', html)
        return m.group(1) if m else ""

    # ------------------------------------------------------------ 其它操作

    def reboot(self):
        """让话机重启。重启后会立刻重新 REGISTER（改完配置不想等 30 分钟时用）。"""
        _, _, html = self.get("/servlet?p=settings-upgrade&q=load")
        token = self.page_token(html)
        path = ("/servlet?p=settings-upgrade&q=reboot"
                "&token=%s&Rajax=%s" % (token, random.random()))
        status, _, body = self.get(path)
        return status, re.sub(r"\s+", " ", body)[:200]

    @staticmethod
    def parse_form(html, form_name="formInput"):
        """从页面里抠出主表单的全部字段（含 select 的选中值）。

        先找 form_name 指定的表单；找不到就退而求其次，挑字段最多的那个表单
        ——话机有些页面主表单不叫 formInput，硬认名字会直接失败。
        """
        candidates = []
        for m in re.finditer(r'<form[^>]*>', html, re.I):
            tag = m.group(0)
            nm = re.search(r'name="([^"]+)"', tag)
            candidates.append((nm.group(1) if nm else "", m.start()))
        if not candidates:
            raise YealinkError("页面上没有任何 form")

        chosen = None
        for name, start in candidates:
            if name == form_name:
                chosen = start
                break
        if chosen is None:
            best, best_count = None, -1
            for _name, start in candidates:
                chunk = html[start:start + 200000]
                count = len(re.findall(r'name="', chunk[:60000]))
                if count > best_count:
                    best, best_count = start, count
            chosen = best

        # 表单结束位置：从起点往后找第一个 </form>
        end = html.find("</form>", chosen)
        form = html[chosen:end if end > 0 else len(html)]

        fields = {}
        for im in re.finditer(r"<input[^>]*>", form, re.I):
            tag = im.group(0)
            nm = re.search(r'name="([^"]+)"', tag)
            if not nm:
                continue
            name = nm.group(1)
            if re.search(r'type="(button|submit|reset)"', tag, re.I):
                continue
            if re.search(r'type="(checkbox|radio)"', tag, re.I):
                # 没勾选的 checkbox 不提交；勾选的提交它的 value
                fields[name] = "" if not re.search(r"\bchecked\b", tag, re.I) else \
                    (re.search(r'value="([^"]*)"', tag) or [None, "1"])[1]
                continue
            val = re.search(r'value="([^"]*)"', tag)
            fields[name] = val.group(1) if val else ""
        for sm in re.finditer(r'<select[^>]*name="([^"]+)"[^>]*>(.*?)</select>', form, re.S | re.I):
            name, body = sm.group(1), sm.group(2)
            value = ""
            for om in re.finditer(r"<option[^>]*>", body, re.I):
                tag = om.group(0)
                if re.search(r"\bselected\b", tag, re.I):   # value 和 selected 的先后顺序不固定
                    v = re.search(r'value="([^"]*)"', tag)
                    value = v.group(1) if v else ""
                    break
            fields[name] = value
        return fields

    # ------------------------------------------------- 通用页面读写（任意设置页）

    def load_form(self, page, acc=None):
        path = "/servlet?p=%s&q=load" % page
        if acc is not None:
            path += "&acc=%d" % acc
        status, _, html = self.get(path)
        if status != 200 or len(html) < 500:
            raise YealinkError("读取 %s 失败 status=%s len=%d" % (path, status, len(html)))
        fields = self.parse_form(html)
        fields["token"] = self.page_token(html)
        return fields

    def write_form(self, page, changes, acc=None, verify=None):
        """改话机任意设置页上的字段。

        changes: {"EnableAutoAnswerTone": "0"}
        返回 (改动前的字段, 提交的字段)。verify: 改完后要回读确认的字段名列表。
        """
        suffix = "&acc=%d" % acc if acc is not None else ""
        status, _, html = self.get("/servlet?p=%s&q=load%s" % (page, suffix))
        if status != 200 or len(html) < 500:
            raise YealinkError("读取 %s 失败 status=%s" % (page, status))
        fields = self.parse_form(html)
        token = self.page_token(html)
        if not token:
            raise YealinkError("页面上没读到 g_strToken，无法通过身份校验")
        fields["token"] = token
        before = dict(fields)
        fields.update({k: str(v) for k, v in changes.items()})

        status, _, body = self.post("/servlet?p=%s&q=write%s" % (page, suffix), fields)
        if status not in (200, 301, 302, 303):
            raise YealinkError("写入 %s 失败 status=%s body=%r" % (page, status, body[:200]))

        if verify:
            now = self.load_form(page, acc)
            bad = [k for k in verify if now.get(k) != str(changes.get(k))]
            if bad:
                raise YealinkError("写入后回读不一致：%s"
                                   % ", ".join("%s=%s(期望 %s)"
                                               % (k, now.get(k), changes.get(k)) for k in bad))
        return before, fields

    def set_auto_answer(self, seconds, acc=0, enable=True):
        """打开**话机自己**的"自动应答延迟"。

        为什么需要它：服务端发 `Call-Info: answer-after=N` 那种方式，
        话机在延迟期间**不播正常来电铃声**（只放一声自动接听提示音；
        把提示音关了就是静音干等）。想让话机像接普通来电那样正常响铃，
        就得让话机自己计时：

            AccountAutoAnswerSwitch = 1     （账号级开关）
            AutoAnswerDelay         = N     （响铃多少秒后自动接起）

        这样 bridge 一个头都不用发，INVITE 就是普通来电。
        """
        features = self.write_form("features-general",
                                   {"AutoAnswerDelay": str(seconds)},
                                   verify=["AutoAnswerDelay"])
        account = self.write_form("account-basic",
                                  {"AccountAutoAnswerSwitch": "1" if enable else "0"},
                                  acc=acc,
                                  verify=["AccountAutoAnswerSwitch"])
        return {"features-general": features, "account-basic": account}

    def account_page(self, acc):
        status, _, html = self.get(ACCOUNT_PAGE % acc)
        if status != 200 or len(html) < 500:
            raise YealinkError("读取账号页失败 acc=%d status=%s len=%d"
                               % (acc, status, len(html)))
        return html

    def show_account(self, acc=0):
        html = self.account_page(acc)
        fields = self.parse_account_form(html)
        fields["token"] = self.page_token(html)
        return fields

    def set_account(self, acc, ext, server, port=5060, label="codex",
                    password="local-phone", enable=True, expires=60):
        """把某个账号配成本机 PBX。返回 (写之前的字段, 写之后的字段)。

        expires 默认 60 秒：话机会按一半的时间（约 30 秒）重新注册一次。
        这样本机服务重启后，最多等半分钟话机就自己回来了，不用手动去拨电话。
        嫌吵可以调大，但别超过 3600。
        """
        html = self.account_page(acc)
        fields = self.parse_account_form(html)
        fields["token"] = self.page_token(html)
        if not fields["token"]:
            raise YealinkError("页面上没读到 g_strToken，无法通过身份校验")
        before = dict(fields)
        fields.update({
            "AccountEnable": "1" if enable else "0",
            "AccountLabel": label,
            "AccountDisplayName": label,
            "AccountRegisterName": ext,
            "AccountUserName": ext,
            "AccountPassword": password,
            "server1": server,
            "port1": str(port),
            "transport1": "0",          # 0=UDP
            "expires1": str(expires),
            "RetryCounts1": fields.get("RetryCounts1") or "3",
            "server2": "",
            "port2": "5060",
            "OutboundHost1": "",
            "OutboundPort1": "5060",
            "OutboundHost2": "",
            "OutboundPort2": "5060",
            "AccountOutboundSwitch": "0",
            "AccountStunSwitch": "0",   # 同一局域网，必须关
            "AccountProxyFallbackInterval": fields.get("AccountProxyFallbackInterval") or "3600",
            "var_accountID": str(acc),
        })
        status, _, body = self.post(ACCOUNT_WRITE % acc, fields)
        if status not in (200, 301, 302, 303):
            raise YealinkError("写入失败 status=%s body=%r" % (status, body[:200]))
        return before, fields


def _cli():
    ip = DEFAULT_IP
    if not ip:
        raise YealinkError("Set CODEX_PHONE_IP, or use PhoneService.exe --phone-ip <IP> instead")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    phone = YealinkPhone(ip)
    print("登录 %s ..." % ip)
    phone.login()
    print("  OK\n")

    if cmd == "show":
        acc = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        fields = phone.show_account(acc)
        print("=== Account%d（acc=%d）当前值 ===" % (acc + 1, acc))
        for k in ACCOUNT_FIELDS:
            if k in fields:
                print("  %-30s = %s" % (k, "[redacted]" if "password" in k.lower() else fields[k]))
        return 0

    if cmd == "set":
        acc, ext, server = int(sys.argv[2]), sys.argv[3], sys.argv[4]
        port = int(sys.argv[5]) if len(sys.argv) > 5 else 5060
        expires = int(sys.argv[6]) if len(sys.argv) > 6 else 60
        before, after = phone.set_account(acc, ext, server, port, expires=expires)
        print("=== 改之前 ===")
        for k in ACCOUNT_FIELDS:
            print("  %-30s = %s" % (k, "[redacted]" if "password" in k.lower() else before.get(k, "")))
        print("\n=== 已下发 ===")
        for k in ACCOUNT_FIELDS:
            print("  %-30s = %s" % (k, "[redacted]" if "password" in k.lower() else after.get(k, "")))
        print("\n回读验证 ...")
        now = phone.show_account(acc)
        ok = (now.get("server1") == server and now.get("AccountUserName") == ext
              and now.get("port1") == str(port) and now.get("AccountEnable") == "1")
        for k in ("AccountEnable", "AccountUserName", "server1", "port1", "transport1"):
            print("  %-30s = %s" % (k, now.get(k, "")))
        print("\n结果:", "通过" if ok else "看起来没写进去，再看一眼话机屏幕")
        return 0 if ok else 1

    if cmd == "reboot":
        st, body = phone.reboot()
        print("重启指令已发送 status=%s %s" % (st, body))
        print("话机大约 40~60 秒后回来，并会立刻重新注册。")
        return 0

    if cmd == "fields":
        page = sys.argv[2] if len(sys.argv) > 2 else "features-general"
        acc = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else None
        fields = phone.load_form(page, acc)
        print("=== %s%s 的字段 ===" % (page, "" if acc is None else " acc=%d" % acc))
        for k, v in fields.items():
            if k == "token":
                continue
            print("  %-38s = %s" % (k, v))
        return 0

    if cmd == "setf":
        page = sys.argv[2]
        pairs = [a for a in sys.argv[3:] if "=" in a and not a.startswith("--")]
        acc = None
        for a in sys.argv[3:]:
            if a.startswith("--acc="):
                acc = int(a.split("=", 1)[1])
        changes = {}
        for p in pairs:
            k, v = p.split("=", 1)
            changes[k] = v
        if not changes:
            print("要给至少一个 k=v，例如：setf features-general EnableAutoAnswerTone=0")
            return 2
        before, after = phone.write_form(page, changes, acc=acc, verify=list(changes))
        print("=== %s 改动 ===" % page)
        for k, v in changes.items():
            print("  %-38s %s -> %s" % (k, before.get(k, "(无)"), v))
        print("\n回读确认：全部一致 ✅")
        return 0

    if cmd == "autoanswer":
        seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 6
        acc = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        phone.set_auto_answer(seconds, acc=acc)
        print("已设置：Account%d 打开自动应答，响铃 %d 秒后自动接起。" % (acc + 1, seconds))
        print("  注意：这样话机响的是**正常来电铃声**（因为 bridge 不再发自动接听头）。")
        return 0

    if cmd == "noautoanswer":
        acc = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        phone.set_auto_answer(0, acc=acc, enable=False)
        print("已关闭 Account%d 的自动应答，来电会一直响到你手动接。" % (acc + 1))
        return 0

    print("用法:\n"
          "  python yealink_web.py show [acc]\n"
          "  python yealink_web.py set <acc> <分机号> <服务器IP> [端口] [有效期]\n"
          "  python yealink_web.py fields <页面> [acc]\n"
          "  python yealink_web.py setf <页面> Key=Value [Key=Value ...] [--acc=N]\n"
          "  python yealink_web.py autoanswer <秒> [acc]   # 话机自己响铃N秒后自动接\n"
          "  python yealink_web.py noautoanswer [acc]\n"
          "  python yealink_web.py reboot")
    return 2


if __name__ == "__main__":
    sys.exit(_cli())
