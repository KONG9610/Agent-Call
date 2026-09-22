"""G.711 u-law / A-law codec in pure Python.

Needed because Python 3.13 removed the stdlib `audioop` module (PEP 594),
and G.711 is the only codec every IP phone is guaranteed to support.
"""

import array

_ULAW_BIAS = 0x84
_ULAW_CLIP = 32635

_ALAW_SEG_END = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)
_QUANT_MASK = 0x0F
_SEG_MASK = 0x70
_SEG_SHIFT = 4
_SIGN_BIT = 0x80

ULAW = "ulaw"
ALAW = "alaw"

_cache = {}


def lin2ulaw(sample):
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > _ULAW_CLIP:
        sample = _ULAW_CLIP
    sample += _ULAW_BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def ulaw2lin(u):
    u = (~u) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = ((mantissa << 3) + _ULAW_BIAS) << exponent
    sample -= _ULAW_BIAS
    return -sample if sign else sample


def lin2alaw(sample):
    sample >>= 3
    if sample >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        sample = -sample - 1
    if sample > 0x0FFF:
        sample = 0x0FFF
    seg = 8
    for i, end in enumerate(_ALAW_SEG_END):
        if sample <= end:
            seg = i
            break
    if seg >= 8:
        return 0x7F ^ mask
    aval = seg << _SEG_SHIFT
    if seg < 2:
        aval |= (sample >> 1) & _QUANT_MASK
    else:
        aval |= (sample >> seg) & _QUANT_MASK
    return aval ^ mask


def alaw2lin(a):
    a ^= 0x55
    t = (a & _QUANT_MASK) << 4
    seg = (a & _SEG_MASK) >> _SEG_SHIFT
    if seg == 0:
        t += 8
    elif seg == 1:
        t += 0x108
    else:
        t += 0x108
        t <<= seg - 1
    return t if (a & _SIGN_BIT) else -t


def _tables(law):
    """Build (or fetch) the 256-entry decode and 65536-entry encode tables."""
    if law in _cache:
        return _cache[law]

    if law == ULAW:
        decode_fn, encode_fn = ulaw2lin, lin2ulaw
    elif law == ALAW:
        decode_fn, encode_fn = alaw2lin, lin2alaw
    else:
        raise ValueError("unknown law: %r" % (law,))

    decode = [decode_fn(i) for i in range(256)]
    # Indexed by (sample & 0xFFFF) so negative samples do not wrap around.
    encode = bytearray(65536)
    for s in range(-32768, 32768):
        encode[s & 0xFFFF] = encode_fn(s)

    _cache[law] = (decode, encode)
    return _cache[law]


def decode(data, law=ALAW):
    """G.711 bytes -> 16-bit little-endian linear PCM bytes."""
    decode_tbl, _ = _tables(law)
    out = array.array("h", bytes(2 * len(data)))
    i = 0
    for b in data:
        out[i] = decode_tbl[b]
        i += 1
    return out.tobytes()


def encode(pcm, law=ALAW):
    """16-bit little-endian linear PCM bytes -> G.711 bytes."""
    _, encode_tbl = _tables(law)
    n = len(pcm) // 2
    samples = array.array("h")
    samples.frombytes(pcm[: n * 2])
    return bytes(encode_tbl[s & 0xFFFF] for s in samples)


def silence(payload_type, law=ALAW, samples=160):
    """A frame of digital silence, already encoded."""
    return bytes(encode(b"\x00\x00" * samples, law))


def resample(pcm, src_rate, dst_rate):
    """Crude but serviceable box-filter resample for speech."""
    if src_rate == dst_rate:
        return pcm
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) // 2 * 2])
    n_src = len(src)
    n_dst = int(n_src * dst_rate / src_rate)
    if n_dst <= 0:
        return b""
    out = array.array("h", bytes(2 * n_dst))
    ratio = src_rate / dst_rate
    for i in range(n_dst):
        lo = int(i * ratio)
        hi = max(lo + 1, int((i + 1) * ratio))
        hi = min(hi, n_src)
        total = 0
        for j in range(lo, hi):
            total += src[j]
        out[i] = int(total / (hi - lo))
    return out.tobytes()
