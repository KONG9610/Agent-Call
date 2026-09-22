"""voice.py - text <-> speech adapters (TTS and ASR) with zero-cost defaults.

TTS 有两个后端，用 config.json 的 `voice.backend` 切换：

  · `sapi`（默认）——Windows 自带的语音引擎，经 PowerShell 调用。
    零安装、离线可用，但中文通常只有"Microsoft Huihui"一个嗓子。

  · `edge` ——微软 Edge 浏览器"朗读"功能背后那套在线音色，需要装
    `edge-tts` + `soundfile`，**且每次合成都联网**。中文有十几个音色（含男声）。
    ⚠️ 这是逆向出来的非官方接口，微软随时可能改协议或限速。

两条路最后都产出 8 kHz 单声道 16-bit PCM —— 也就是话机实际能播的东西。
12 秒以内的句子会缓存在 `cache/`，同样的文本 + 同样的设置不会再合成第二次
（所以固定口令只有第一通要等，后面都是直接放的）。

ASR 优先用 faster-whisper，没有就退化成"把 wav 留着"，不会报错。
"""

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import wave

import rtp

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")

_tts_lock = threading.Lock()
_asr_model = None
_asr_lock = threading.Lock()


# ----------------------------------------------------------------------- TTS

# 当前生效的 TTS 设置。bridge.py 启动时用 configure() 灌进来；
# 直接手跑脚本（tts_check.py / voice.py）时就用这里的默认值。
_settings = {
    "backend": "sapi",        # "sapi" 或 "edge"
    "voice": "",              # sapi: 音色名（任意子串匹配）; edge: ShortName 全名
    "rate": 0,                # 语速。edge 是 -100..+100 (%)，sapi 会折算到 -10..+10
    "pitch": 0,               # 音调，只在 edge 后端有效，单位 Hz（-100..+100）
    "volume": 0,              # 音量 (%)，-100..+100
}

EDGE_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

_EDGE_VOICE_CACHE = {"at": 0.0, "items": []}
_EDGE_VOICE_TTL = 600.0       # 音色列表 10 分钟刷新一次，别每次开控制台都打网络


def configure(cfg):
    """把 config.json 的 `voice` 段灌进来（bridge 启动时调一次）。

    只认识的键才覆盖，缺的保持默认 —— 老的配置文件里没有 backend 也不会炸。
    返回生效后的设置，方便 bridge 打横幅。

    注意音色这个键在配置里叫 `tts_voice`（历史命名），模块内部叫 `voice`，
    这里两个名字都认，`tts_voice` 优先 —— 否则"控制台里选了音色、保存了、
    可一调用还是默认嗓子"这种问题会很难查。
    """
    if isinstance(cfg, dict):
        for key in ("backend", "voice", "rate", "pitch", "volume"):
            if cfg.get(key) is not None:
                _settings[key] = cfg[key]
        if cfg.get("tts_voice") is not None:
            _settings["voice"] = cfg["tts_voice"]
    if str(_settings.get("backend") or "sapi").lower() not in ("sapi", "edge"):
        _settings["backend"] = "sapi"
    return dict(_settings)


def settings():
    """看一眼当前生效的设置（只读副本）。"""
    return dict(_settings)


def _find_powershell():
    """找到 powershell.exe。

    只查 PATH 是不够的：bridge.py 常被后台/计划任务拉起，那时的 PATH 可能被裁剪，
    而 Windows PowerShell 装在 System32\\WindowsPowerShell\\v1.0 下，**不在 System32 里**，
    于是 shutil.which 找不到 -> list_voices() 返回空 -> TTS 静音。
    所以这里补上几个固定位置的兜底。
    """
    for name in ("powershell.exe", "pwsh.exe"):
        path = shutil.which(name)
        if path:
            return path

    root = os.environ.get("SystemRoot") or r"C:\Windows"
    candidates = [
        os.path.join(root, r"System32\WindowsPowerShell\v1.0\powershell.exe"),
        os.path.join(root, r"SysWOW64\WindowsPowerShell\v1.0\powershell.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     r"PowerShell\7\pwsh.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     r"PowerShell\6\pwsh.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _ps_quote(text):
    return text.replace("'", "''")


# PowerShell 在中文 Windows 上默认按 GBK 往 stdout 写，Python 按 UTF-8 解会炸在
# 读线程里（UnicodeDecodeError，报错还不在主线程，很难查）。所以两步都做：
#   ① 脚本开头强制它按 UTF-8 输出；
#   ② subprocess 再挂个 errors="replace" 兜底 —— 万一还是解不了，也不至于炸。
_PS_UTF8 = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "


def _run_ps(ps, script, timeout=120):
    proc = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-Command", _PS_UTF8 + script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return proc


def list_voices():
    ps = _find_powershell()
    if not ps:
        return []
    try:
        out = _run_ps(
            ps,
            "Add-Type -AssemblyName System.Speech; "
            "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
            ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }",
            timeout=30)
        return [v.strip() for v in (out.stdout or "").splitlines() if v.strip()]
    except (OSError, subprocess.SubprocessError):
        return []


def _pick_voice(preferred):
    voices = list_voices()
    if not voices:
        return None
    if preferred:
        for v in voices:
            if preferred.lower() in v.lower():
                return v
    for v in voices:
        name = v.lower()
        if "chinese" in name or "zh-cn" in name or "huihui" in name or "yaoyao" in name:
            return v
    return voices[0]


def _sapi_script(text, voice, rate, volume, out_path):
    """拼出 SAPI 的 PowerShell 脚本。

    rate / volume 为 0 时整段不生成，保证"不带任何设置"时脚本和以前一模一样。
    """
    parts = [
        "Add-Type -AssemblyName System.Speech; ",
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; ",
        "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
        "8000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
        "[System.Speech.AudioFormat.AudioChannel]::Mono); ",
    ]
    if voice:
        parts.append("$s.SelectVoice('%s'); " % _ps_quote(voice))
    if rate:
        # SAPI 的 Rate 是 -10..10；我们的 rate 是百分比，除以 10 折算过去
        parts.append("$s.Rate = %d; " % max(-10, min(10, int(round(rate / 10.0)))))
    if volume:
        parts.append("$s.Volume = %d; " % max(0, min(100, int(100 + volume))))
    parts.append("$s.SetOutputToWaveFile('%s', $fmt); " % _ps_quote(out_path))
    parts.append("$s.Speak('%s'); " % _ps_quote(text))
    parts.append("$s.Dispose()")
    return "".join(parts)


def _sapi_synthesize(text, voice, rate, volume, out_path):
    """Windows 自带引擎 -> 直接写 8 kHz 单声道 WAV。"""
    ps = _find_powershell()
    if not ps:
        raise RuntimeError("PowerShell not found; cannot run Windows TTS")
    use_voice = voice or _pick_voice(None)
    script = _sapi_script(text, use_voice, rate, volume, out_path)
    proc = _run_ps(ps, script, timeout=120)
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError("TTS failed: %s %s"
                           % ((proc.stdout or "").strip(),
                              (proc.stderr or "").strip()))
    return use_voice or ""


# ------------------------------------------------------------------ edge 后端

def edge_voices(force=False):
    """列出 Edge 在线音色。拿不到就返回空列表，绝不抛异常。

    返回 [{"name", "gender", "locale", "friendly"}, ...]。
    结果缓存 10 分钟 —— 这东西要联网，控制台不该每刷一次就打一次网络。
    """
    import time
    now = time.time()
    if not force and _EDGE_VOICE_CACHE["items"] \
            and now - _EDGE_VOICE_CACHE["at"] < _EDGE_VOICE_TTL:
        return list(_EDGE_VOICE_CACHE["items"])
    try:
        import asyncio

        import edge_tts

        result = edge_tts.list_voices()
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
    except Exception:
        # 没装、断网、被墙 —— 都走这里。控制台会显示"Edge 不可用"。
        return list(_EDGE_VOICE_CACHE["items"])

    items = []
    for v in result or []:
        name = v.get("ShortName") or ""
        if not name:
            continue
        items.append({
            "name": name,
            "gender": v.get("Gender") or "",
            "locale": v.get("Locale") or "",
            "friendly": v.get("FriendlyName") or name,
        })
    items.sort(key=lambda x: (not x["locale"].lower().startswith("zh"), x["locale"], x["name"]))
    _EDGE_VOICE_CACHE["at"] = now
    _EDGE_VOICE_CACHE["items"] = items
    return list(items)


def _edge_save_mp3(text, voice, rate, pitch, volume, mp3_path):
    """调 Edge 接口合成 MP3。返回实际用的音色名。

    ⚠️ 输出格式是 edge-tts 写死的 `audio-24khz-48kbitrate-mono-mp3`：
    源码里 `speech.config` 那段是字符串字面量，没有参数能改成 PCM。
    所以想拿到 PCM 只能自己解 MP3 —— 见 _mp3_to_wav()。
    """
    import asyncio

    try:
        import edge_tts
    except ImportError:
        raise RuntimeError("edge 后端没装依赖：pip install edge-tts soundfile")

    use_voice = voice or EDGE_DEFAULT_VOICE
    # rate/volume 收百分比字符串，pitch 收 Hz —— 传错格式 edge-tts 会直接抛
    comm = edge_tts.Communicate(
        text, use_voice,
        rate="%+d%%" % int(rate or 0),
        volume="%+d%%" % int(volume or 0),
        pitch="%+dHz" % int(pitch or 0),
    )
    # edge-tts 7.x 有同步入口；老版本只有 async 的 save()
    save_sync = getattr(comm, "save_sync", None)
    if save_sync is not None:
        save_sync(mp3_path)
    else:
        asyncio.run(comm.save(mp3_path))

    if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) == 0:
        raise RuntimeError("edge-tts 没有产出音频（接口变了？网络断了？）")
    return use_voice


def _mp3_to_wav(mp3_path, wav_path):
    """MP3 -> WAV（保持原始采样率，通常是 24 kHz）。返回采样率。

    libsndfile >= 1.1 才能读 MP3，soundfile 的 wheel 里自带的版本够新。
    """
    try:
        import soundfile as sf
    except ImportError:
        raise RuntimeError("edge 后端没装依赖：pip install edge-tts soundfile")

    data, rate = sf.read(mp3_path, dtype="int16", always_2d=True)
    if data.shape[1] > 1:                      # 万一给了立体声，混成单声道
        data = data.mean(axis=1)
    else:
        data = data[:, 0]
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(rate))
        wf.writeframes(data.astype("int16").tobytes())
    return int(rate)


def _edge_synthesize(text, voice, rate, pitch, volume, out_path):
    """Edge 在线音色 -> WAV（Edge 给的是 24 kHz，随后由 read_wav_pcm 重采样到 8 kHz）。"""
    mp3_path = out_path[:-4] + ".mp3" if out_path.lower().endswith(".wav") \
        else out_path + ".mp3"
    try:
        use_voice = _edge_save_mp3(text, voice, rate, pitch, volume, mp3_path)
        _mp3_to_wav(mp3_path, out_path)
        return use_voice
    finally:
        # 中间产物不留在 cache/ 里，只有 wav 会留着
        if os.path.exists(mp3_path):
            try:
                os.remove(mp3_path)
            except OSError:
                pass


def synthesize(text, voice=None, cache=True, backend=None, rate=None,
               pitch=None, volume=None):
    """Text -> 8 kHz mono 16-bit PCM bytes.

    backend / voice / rate / pitch / volume 都不传就用 configure() 灌进来的全局设置。
    显式传值优先 —— /notify 接口靠这个让"控制台里还没保存的试听设置"也能直接拨出去。
    """
    if not text:
        return b""

    backend = str(backend or _settings.get("backend") or "sapi").lower()
    if backend not in ("sapi", "edge"):
        backend = "sapi"
    use_voice = voice if voice else (_settings.get("voice") or "")
    rate = int(_settings.get("rate") or 0) if rate is None else int(rate)
    pitch = int(_settings.get("pitch") or 0) if pitch is None else int(pitch)
    volume = int(_settings.get("volume") or 0) if volume is None else int(volume)

    os.makedirs(CACHE_DIR, exist_ok=True)
    # 缓存键必须带上全部设置：同一句话换了音色/语速就是另一段音频。
    key_src = "|".join([text, backend, str(use_voice),
                        str(rate), str(pitch), str(volume)])
    key = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:16]
    wav_path = os.path.join(CACHE_DIR, key + ".wav")

    with _tts_lock:
        if not (cache and os.path.exists(wav_path)):
            if backend == "edge":
                _edge_synthesize(text, use_voice, rate, pitch, volume, wav_path)
            else:
                _sapi_synthesize(text, use_voice, rate, volume, wav_path)

    return rtp.read_wav_pcm(wav_path, target_rate=rtp.SAMPLE_RATE)



# ----------------------------------------------------------------------- ASR

def _load_asr(model_size="small", device="cpu", compute_type="int8"):
    global _asr_model
    with _asr_lock:
        if _asr_model is not None:
            return _asr_model
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return None
        _asr_model = WhisperModel(model_size, device=device,
                                  compute_type=compute_type)
        return _asr_model


def transcribe(pcm, language="zh", model_size="small", keep_wav=False):
    """8 kHz mono 16-bit PCM -> text. Returns None when no engine is available."""
    if not pcm:
        return None

    model = _load_asr(model_size)
    if model is None:
        if keep_wav:
            path = os.path.join(CACHE_DIR, "last_utterance.wav")
            os.makedirs(CACHE_DIR, exist_ok=True)
            rtp.write_wav_pcm(path, pcm)
        return None

    os.makedirs(CACHE_DIR, exist_ok=True)
    wav_path = os.path.join(CACHE_DIR, "last_utterance.wav")
    rtp.write_wav_pcm(wav_path, pcm)
    segments, _info = model.transcribe(wav_path, language=language,
                                       beam_size=5, vad_filter=True)
    return "".join(seg.text for seg in segments).strip()


def asr_backend_name():
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return None
    return "faster-whisper"


if __name__ == "__main__":
    import argparse
    import time

    ap = argparse.ArgumentParser(
        description="TTS 自测：不传参数就跑一句默认话，两个后端都能试")
    ap.add_argument("text", nargs="?",
                    default="测试一下，我是 WorkBuddy，任务已经完成了。")
    ap.add_argument("--backend", default="sapi", choices=["sapi", "edge"])
    ap.add_argument("--voice", default="", help="sapi: 子串; edge: ShortName 全名")
    ap.add_argument("--rate", type=int, default=0, help="语速 -100..100")
    ap.add_argument("--pitch", type=int, default=0, help="音调 Hz，仅 edge")
    ap.add_argument("--volume", type=int, default=0, help="音量 -100..100，仅 edge")
    ap.add_argument("--out", default="", help="顺便把 8k PCM 存成 wav")
    ap.add_argument("--list", action="store_true", help="列出两个后端的音色")
    args = ap.parse_args()

    print("sapi voices :", list_voices())
    if args.list:
        items = edge_voices(force=True)
        print("edge voices : %d 个" % len(items))
        for v in items:
            if v["locale"].lower().startswith("zh"):
                print("   %-30s %-6s %s" % (v["name"], v["gender"], v["locale"]))
        raise SystemExit(0)

    t0 = time.time()
    pcm = synthesize(args.text, voice=args.voice or None, backend=args.backend,
                     rate=args.rate, pitch=args.pitch, volume=args.volume)
    print("%s 后端 %d 字节 8 kHz PCM（%.2f 秒），耗时 %.2f 秒"
          % (args.backend, len(pcm), len(pcm) / 2.0 / rtp.SAMPLE_RATE,
             time.time() - t0))
    if args.out:
        rtp.write_wav_pcm(args.out, pcm)
        print("已写出:", args.out)
    print("asr backend:", asr_backend_name() or "none (pip install faster-whisper)")
