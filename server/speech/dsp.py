"""Звук без синтезатора: телефонная линия, звонок, WAV.

Телефонная линия — numpy (он есть там, где есть onnxruntime, то есть в процессе озвучки).
Звонок и запись WAV — чистый Python: сервер делает их без пакетов озвучки.
"""
from __future__ import annotations

import math
import os
import wave
from array import array
from pathlib import Path

PHONE_RATE = 8000
ALAW_A = 87.6


def write_wav(path, pcm16, rate: int) -> None:
    """Моно, 16 бит. Запись через временный файл: сервер никогда не отдаст недописанный звук."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp%d" % os.getpid())
    data = pcm16.tobytes() if hasattr(pcm16, "tobytes") else bytes(pcm16)
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)
    os.replace(tmp, path)


def ring_wav(path, rate: int = PHONE_RATE) -> None:
    """Звонок телефона оператора: две трели по 0,4 с (800/1000 Гц, 16 Гц), затем тишина — всего 2 с,
    в интерфейсе звучит по кругу, пока вызов не принят."""
    out = array("h")
    for i in range(int(2.0 * rate)):
        t = i / rate
        burst = (t % 0.6) < 0.4 and t < 1.2
        if not burst:
            out.append(0)
            continue
        freq = 800.0 if int(t * 32) % 2 == 0 else 1000.0
        edge = min(1.0, (t % 0.6) / 0.01, (0.4 - t % 0.6) / 0.01)
        out.append(int(0.45 * 32767 * edge * math.sin(2 * math.pi * freq * t)))
    write_wav(path, out, rate)


# ---------- телефонная линия (numpy) ----------
def resample(x, rate: int, out_rate: int):
    """Передискретизация через спектр: для коротких реплик быстро и без внешних библиотек."""
    import numpy as np
    n = len(x)
    if n == 0 or rate == out_rate:
        return np.asarray(x, dtype=np.float64)
    m = int(round(n * out_rate / rate))
    spec = np.fft.rfft(np.asarray(x, dtype=np.float64))
    if m // 2 + 1 <= len(spec):
        spec = spec[: m // 2 + 1]
    else:
        spec = np.concatenate([spec, np.zeros(m // 2 + 1 - len(spec), dtype=spec.dtype)])
    return np.fft.irfft(spec, m) * (m / n)


def alaw_roundtrip(y):
    """Квантование по A-закону (G.711) туда и обратно: так звук проходит через цифровую телефонную линию."""
    import numpy as np
    a, ln = ALAW_A, 1.0 + math.log(ALAW_A)
    s, x = np.sign(y), np.minimum(np.abs(y), 1.0)
    f = np.where(x < 1 / a, a * x / ln, (1 + np.log(np.maximum(a * x, 1e-12))) / ln)
    f = np.round(f * 127) / 127                                   # 8 бит на отсчёт
    back = np.where(f < 1 / ln, f * ln / a, np.exp(f * ln - 1) / a)
    return s * back


def phone_line(x, rate: int, out_rate: int = PHONE_RATE, alaw: bool = True):
    """Полоса 300–3400 Гц, 8 кГц, A-закон, уровень −1 дБ, мягкие края. Возвращает int16."""
    import numpy as np
    x = np.asarray(x, dtype=np.float64)
    n = len(x)
    if n == 0:
        return np.zeros(0, dtype="<i2")
    spec = np.fft.rfft(x)
    freq = np.fft.rfftfreq(n, 1.0 / rate)
    spec *= np.clip((freq - 250.0) / 100.0, 0, 1) * np.clip((3500.0 - freq) / 200.0, 0, 1)
    m = int(round(n * out_rate / rate))
    y = np.fft.irfft(spec[: m // 2 + 1], m) * (m / n)
    peak = float(np.max(np.abs(y))) or 1.0
    y = y / peak * 0.89
    fade = min(len(y) // 2, int(0.01 * out_rate))
    if fade:
        ramp = np.linspace(0.0, 1.0, fade)
        y[:fade] *= ramp
        y[-fade:] *= ramp[::-1]
    if alaw:
        y = alaw_roundtrip(y)
    return np.clip(np.round(y * 32767), -32768, 32767).astype("<i2")
