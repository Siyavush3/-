"""Распознавание речи оператора 112 (узел 9): Vosk, малая русская модель, словарь разговора оператора.

Модель одна на сервер (~170 МБ памяти), распознаватель — на каждый открытый микрофон (меньше 1 МБ).
Словарь ограничен словами разговора оператора (data/asr/vocabulary.txt — леммы, словоформы добавляет
pymorphy3): нужен вопрос заявителю, а не диктант. Замер на 4 ядрах: 20 операторов говорят одновременно
без пауз — 0,6 ядра, распознавание не отстаёт от речи; со всем словарём модели — все 4 ядра и отставание
до 2,5 с. Вопрос по фразам с шумом определяется не хуже (49 из 54 против 47).
Распознавание идёт в своём пуле потоков с пониженным приоритетом (Vosk отпускает GIL на время расчёта):
цикл событий сервера и занятия его не ждут.

Конец фразы — пауза 0,5 с после речи (по громкости, порог — над уровнем фонового шума). Своё правило нужно
потому, что со словарём оператора правила конца фразы Vosk с проверкой уверенности не срабатывают и итог
приходит только после 2 с тишины — для разговора это долго.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
import time
from array import array
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from speech.paths import ascii_dir

MODEL = "vosk-model-small-ru-0.22"
RATE = 16000                 # Гц, 16 бит, моно — так шлёт браузер
MAX_UTTERANCE_S = 20.0       # длиннее — принудительный итог: оператор задаёт вопросы, а не диктует
PARTIAL_EVERY_S = 0.4        # промежуточный текст — не чаще
END_SILENCE_S = 0.5          # пауза после речи — конец фразы (как правило Kaldi для уверенного распознавания)
MIN_SPEECH_S = 0.2           # короче — не речь, а стук или щелчок
SPEECH_RMS = 200             # громкость речи (16 бит) не ниже этой и втрое выше фонового шума
NOISE_RESET_S = 8.0          # громко без перерыва дольше — это шум (вентилятор, гул), а не речь


class AsrUnavailable(RuntimeError):
    """Распознавание сейчас недоступно; текст — причина для оператора."""


def _lower_priority() -> None:
    """Потоки распознавания уступают процессор циклу событий сервера."""
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetThreadPriority(k32.GetCurrentThread(), -1)          # THREAD_PRIORITY_BELOW_NORMAL
        else:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 5)  # в Linux nice действует на поток
    except (OSError, AttributeError):
        pass


def vocabulary(path: Path) -> list:
    """Слова разговора оператора: леммы из файла и все их словоформы (pymorphy3, если установлен)."""
    seeds = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        seeds.extend(w.lower() for w in line.split("#", 1)[0].split())
    words = set(seeds)
    try:
        import pymorphy3
        morph = pymorphy3.MorphAnalyzer()
        for s in seeds:
            for p in morph.parse(s)[:2]:
                words.update(f.word for f in p.lexeme)
    except ImportError:
        pass
    words |= {w.replace("ё", "е") for w in words}
    return sorted(words)


def loudness(pcm: bytes) -> float:
    """Среднеквадратичная громкость куска PCM 16 бит."""
    a = array("h")
    a.frombytes(pcm)
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


def clean(text: str) -> str:
    """Текст распознавания без меток неизвестных слов."""
    return re.sub(r"\s+", " ", (text or "").replace("[unk]", " ")).strip()


class Asr:
    """Распознавание на сервере: модель загружается один раз, микрофоны открывают потоки (Stream)."""

    def __init__(self, model_dir, vocabulary_path, threads: int = 2, max_streams: int = 60):
        self.model_dir = Path(model_dir)
        self.vocabulary_path = Path(vocabulary_path)
        self.threads = max(1, int(threads))
        self.max_streams = int(max_streams)
        self.enabled = True
        self.streams = 0
        self.utterances = 0
        self.audio_s = 0.0
        self.busy_s = 0.0
        self.words = 0
        self.load_s: Optional[float] = None
        self.error: Optional[str] = None
        self._model = None
        self._grammar: Optional[str] = None
        self._loading: Optional[asyncio.Future] = None
        self._pool: Optional[ThreadPoolExecutor] = None

    def installed(self) -> bool:
        try:
            import vosk  # noqa: F401
        except ImportError:
            return False
        return (self.model_dir / "am" / "final.mdl").is_file() and self.vocabulary_path.is_file()

    def status(self) -> dict:
        return {"installed": self.installed(), "enabled": self.enabled, "loaded": self._model is not None,
                "streams": self.streams, "max_streams": self.max_streams, "utterances": self.utterances,
                "audio_s": round(self.audio_s, 1), "load": round(self.busy_s / self.audio_s, 3) if self.audio_s else None,
                "words": self.words, "threads": self.threads, "error": self.error}

    async def run(self, fn, *args):
        if self._pool is None:
            self._pool = ThreadPoolExecutor(self.threads, thread_name_prefix="asr", initializer=_lower_priority)
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def _load(self):
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
        t0 = time.perf_counter()
        model = Model(str(ascii_dir(self.model_dir, "vosk-model")))
        words = vocabulary(self.vocabulary_path)
        grammar = json.dumps([" ".join(words), "[unk]"], ensure_ascii=False)
        return model, grammar, len(words), time.perf_counter() - t0

    async def ready(self) -> bool:
        """Модель загружается при старте сервера или при первом микрофоне — один раз."""
        if self._model is not None:
            return True
        if not self.installed():
            return False
        if self._loading is None:
            self._loading = asyncio.ensure_future(self.run(self._load))
        try:
            self._model, self._grammar, self.words, self.load_s = await asyncio.shield(self._loading)
        except Exception as exc:  # модель повреждена или не читается — показать администратору
            self.error = "%s: %s" % (type(exc).__name__, exc)
            self._loading = None
            return False
        self.error = None
        return True

    async def open(self) -> "Stream":
        if not self.enabled:
            raise AsrUnavailable("распознавание речи выключено администратором")
        if self.streams >= self.max_streams:
            raise AsrUnavailable("все каналы распознавания заняты — задавайте вопросы кнопками")
        self.streams += 1
        try:
            if not await self.ready():
                raise AsrUnavailable("распознавание речи не установлено" if self.error is None else
                                     "распознавание речи не работает: " + self.error)
            from vosk import KaldiRecognizer
            rec = await self.run(KaldiRecognizer, self._model, RATE, self._grammar)
        except BaseException:
            self.streams -= 1
            raise
        return Stream(self, rec)


class Stream:
    """Один микрофон: куски звука → промежуточный текст и итог фразы (конец фразы — по паузе)."""

    def __init__(self, asr: Asr, rec):
        self.asr = asr
        self.rec = rec
        self.samples = 0             # в текущей фразе
        self.spoken = 0              # отсчётов речи в текущей фразе
        self.quiet = 0               # отсчётов тишины после речи
        self.floor = None            # уровень фонового шума
        self.loud_run = 0            # отсчётов подряд громче порога
        self.partial = ""
        self.partial_at = 0.0
        self.closed = False

    async def feed(self, pcm: bytes) -> Optional[dict]:
        if self.closed or len(pcm) < 2:
            return None
        return await self.asr.run(self._feed, bytes(pcm[: len(pcm) // 2 * 2]))

    async def end(self) -> Optional[dict]:
        """Микрофон выключен или вызов окончен: итог того, что успели сказать."""
        if self.closed:
            return None
        return await self.asr.run(self._end)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.asr.streams -= 1

    def _feed(self, pcm: bytes) -> Optional[dict]:
        t0 = time.perf_counter()
        n = len(pcm) // 2
        self.samples += n
        rms = loudness(pcm)
        if self.floor is None:
            self.floor = rms
        loud = rms > max(SPEECH_RMS, 3 * self.floor)
        # Фоновый шум — по паузам: вниз сразу, вверх плавно (~2 с). Во время речи порог не растёт — иначе тихий
        # конец длинной фразы сочтётся тишиной. Громко без перерыва дольше любой реплики — это шум, а не речь.
        if loud:
            self.loud_run += n
            self.spoken += n
            self.quiet = 0
            if self.loud_run > NOISE_RESET_S * RATE:
                self.floor, self.loud_run, self.spoken = rms, 0, 0
        else:
            self.loud_run = 0
            self.floor = rms if rms < self.floor else self.floor + (rms - self.floor) * 0.05
            if self.spoken:
                self.quiet += n
        if self.rec.AcceptWaveform(pcm):
            out = self._final(json.loads(self.rec.Result()))
        elif self.spoken >= MIN_SPEECH_S * RATE and self.quiet >= END_SILENCE_S * RATE:
            out = self._end()                                   # пауза после речи — фраза закончена
        elif self.samples > MAX_UTTERANCE_S * RATE:
            out = self._end()
        else:
            out = None
            now = time.monotonic()
            if now - self.partial_at >= PARTIAL_EVERY_S:
                self.partial_at = now
                text = clean(json.loads(self.rec.PartialResult()).get("partial", ""))
                if text and text != self.partial:
                    self.partial = text
                    out = {"t": "partial", "text": text}
        self.asr.busy_s += time.perf_counter() - t0
        self.asr.audio_s += n / RATE
        return out

    def _end(self) -> Optional[dict]:
        res = json.loads(self.rec.FinalResult())
        self.rec.Reset()
        return self._final(res)

    def _final(self, res: dict) -> Optional[dict]:
        self.samples, self.spoken, self.quiet, self.partial = 0, 0, 0, ""
        text = clean(res.get("text", ""))
        if not text:
            return None
        self.asr.utterances += 1
        return {"t": "final", "text": text}
