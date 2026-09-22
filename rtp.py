"""Minimal RTP audio session over UDP for one G.711 phone call."""

import queue
import random
import socket
import struct
import threading
import time
import wave

import g711

FRAME_MS = 20
SAMPLE_RATE = 8000
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000      # 160
PCM_BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2             # 320
TELEPHONE_EVENT = 101

DTMF_DIGITS = {i: str(i) for i in range(10)}
DTMF_DIGITS[10] = "*"
DTMF_DIGITS[11] = "#"


class RTPSession(object):
    def __init__(self, local_ip, remote_ip, remote_port, payload_type, law,
                 on_dtmf=None, on_audio=None, log=None, port_range=None):
        self.local_ip = local_ip
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self.payload_type = payload_type
        self.law = law
        self.on_dtmf = on_dtmf
        self.on_audio = on_audio
        self.log = log or (lambda *a: None)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._bind(local_ip, port_range)
        self.sock.settimeout(0.4)
        self.local_port = self.sock.getsockname()[1]

        self.seq = random.randrange(0, 0xFFFF)
        self.timestamp = random.randrange(0, 0xFFFFFFFF)
        self.ssrc = random.randrange(0, 0xFFFFFFFF)
        self._silence = g711.silence(payload_type, law, SAMPLES_PER_FRAME)

        self._tx = queue.Queue()
        self._stop = threading.Event()
        self._threads = []
        self._recorder = None
        self._record_lock = threading.Lock()

        self.packets_sent = 0
        self.packets_received = 0
        self.bytes_received = 0
        self.last_rx_at = None
        self.remote_seen = None

    # ---------------------------------------------------------------- lifecycle

    def _bind(self, local_ip, port_range):
        """Prefer a port inside a fixed range so the firewall rule stays simple."""
        if not port_range:
            self.sock.bind((local_ip, 0))
            return
        low, high = int(port_range[0]), int(port_range[1])
        for _ in range(80):
            port = random.randrange(low, high + 1)
            try:
                self.sock.bind((local_ip, port))
                return
            except OSError:
                continue
        self.sock.bind((local_ip, 0))

    def start(self):
        for target in (self._tx_loop, self._rx_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        self.log("RTP bound on %s:%d -> %s:%d (%s)"
                 % (self.local_ip, self.local_port, self.remote_ip,
                    self.remote_port, "PCMA" if self.law == "alaw" else "PCMU"))

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ sending

    def feed_pcm(self, pcm):
        """Queue 8 kHz 16-bit mono PCM; the sender paces it out at 20 ms."""
        for i in range(0, len(pcm) - 1, PCM_BYTES_PER_FRAME):
            frame = pcm[i:i + PCM_BYTES_PER_FRAME]
            if len(frame) < PCM_BYTES_PER_FRAME:
                frame = frame + b"\x00" * (PCM_BYTES_PER_FRAME - len(frame))
            self._tx.put(g711.encode(frame, self.law))

    def flush(self):
        try:
            while True:
                self._tx.get_nowait()
        except queue.Empty:
            pass

    def queued_frames(self):
        return self._tx.qsize()

    def _tx_loop(self):
        next_at = time.monotonic()
        while not self._stop.is_set():
            try:
                payload = self._tx.get_nowait()
            except queue.Empty:
                payload = self._silence
            self._send_payload(payload)
            next_at += FRAME_MS / 1000.0
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_at = time.monotonic()

    def _send_payload(self, payload):
        header = struct.pack("!BBHII", 0x80, self.payload_type & 0x7F,
                             self.seq & 0xFFFF, self.timestamp & 0xFFFFFFFF,
                             self.ssrc)
        try:
            self.sock.sendto(header + payload, (self.remote_ip, self.remote_port))
            self.packets_sent += 1
        except OSError:
            pass
        self.seq = (self.seq + 1) & 0xFFFF
        self.timestamp = (self.timestamp + SAMPLES_PER_FRAME) & 0xFFFFFFFF

    # ---------------------------------------------------------------- receiving

    def _rx_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 12:
                continue
            b0, b1 = data[0], data[1]
            if (b0 >> 6) != 2:
                continue
            cc = b0 & 0x0F
            offset = 12 + 4 * cc
            if b0 & 0x10:                      # header extension
                if len(data) < offset + 4:
                    continue
                ext_words = struct.unpack("!H", data[offset + 2:offset + 4])[0]
                offset += 4 + 4 * ext_words
            if offset >= len(data):
                continue
            end = len(data)
            if b0 & 0x20 and end > offset:     # padding
                end -= data[-1]
            payload = data[offset:end]
            if not payload:
                continue

            payload_type = b1 & 0x7F
            self.packets_received += 1
            self.bytes_received += len(payload)
            self.last_rx_at = time.time()
            self.remote_seen = (addr[0], addr[1])

            if payload_type == TELEPHONE_EVENT:
                self._handle_dtmf(payload)
                continue
            if payload_type != self.payload_type:
                continue

            pcm = g711.decode(payload, self.law)
            with self._record_lock:
                if self._recorder is not None:
                    self._recorder.extend(pcm)
            if self.on_audio:
                self.on_audio(pcm)

    def _handle_dtmf(self, payload):
        if len(payload) < 4:
            return
        event = payload[0]
        is_end = bool(payload[1] & 0x80)
        if is_end and event in DTMF_DIGITS and self.on_dtmf:
            self.on_dtmf(DTMF_DIGITS[event])

    # --------------------------------------------------------------- recording

    def start_recording(self):
        with self._record_lock:
            self._recorder = bytearray()

    def stop_recording(self):
        with self._record_lock:
            data = bytes(self._recorder or b"")
            self._recorder = None
        return data

    def trim_silence(self, pcm, threshold=260, min_ms=200):
        """Drop leading/trailing near-silence so ASR does not get empty audio."""
        import array

        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        keep = 300                                  # 30 ms guard band
        first = 0
        last = len(samples)
        while first < last and abs(samples[first]) < threshold:
            first += 1
        while last > first and abs(samples[last - 1]) < threshold:
            last -= 1
        if last - first < SAMPLE_RATE * min_ms // 1000:
            return b""
        first = max(0, first - keep)
        last = min(len(samples), last + keep)
        return samples[first:last].tobytes()


# ------------------------------------------------------------------ wav helpers

def read_wav_pcm(path, target_rate=SAMPLE_RATE):
    """Load a wav file as mono 16-bit PCM at target_rate."""
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if width != 2:
        raise ValueError("only 16-bit wav is supported, got %d-bit" % (width * 8))

    import array

    samples = array.array("h")
    samples.frombytes(frames)
    if channels > 1:
        mono = array.array("h", bytes(2 * (len(samples) // channels)))
        for i in range(len(mono)):
            base = i * channels
            mono[i] = int(sum(samples[base:base + channels]) / channels)
        samples = mono
    pcm = samples.tobytes()
    return g711.resample(pcm, rate, target_rate) if rate != target_rate else pcm


def write_wav_pcm(path, pcm, rate=SAMPLE_RATE):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
