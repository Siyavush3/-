"""Кто каким голосом говорит, какие фразы озвучить и где лежит готовый звук.

Все реплики известны заранее (сценарий утверждён), поэтому озвучка делается один раз и заранее,
а на занятии сервер только отдаёт файлы. Имя файла — хеш (версия, голос, темп, эффект, текст):
сервер находит звук по тексту реплики сам, не обращаясь к синтезатору.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

VERSION = "v2"            # меняется при смене нормализации текста или обработки звука — кэш пересоздаётся
EFFECT = "phone8k"        # телефонная линия: 300–3400 Гц, 8 кГц, G.711 A-закон
URL_PREFIX = "/tts/"
RING = "ring.wav"
MALE = ("ru_RU-dmitri-medium", "ru_RU-ruslan-medium")
FEMALE = ("ru_RU-irina-medium",)
UNKNOWN = "Не понимаю вас. Повторите, пожалуйста."   # то же, что Caller.UNKNOWN (domain.calls)


@dataclass(frozen=True)
class VoiceSpec:
    voice: str
    scale: float = 1.0    # темп речи: < 1 быстрее, > 1 медленнее (length_scale piper)


BRIGADE = VoiceSpec("ru_RU-ruslan-medium", 1.0)


def _h(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)


def caller_voice(call: dict) -> VoiceSpec:
    """Голос заявителя — по полу; темп у каждого свой (0,92…1,08), взволнованный говорит быстрее."""
    caller = call.get("caller") or {}
    pool = FEMALE if caller.get("gender") == "f" else MALE
    h = _h("%s|%s" % (call.get("key", ""), caller.get("name", "")))
    scale = 0.92 + (h % 17) / 100.0
    if ((call.get("gen") or {}).get("difficulty") or 1) >= 3:
        scale -= 0.06
    return VoiceSpec(pool[h % len(pool)], round(scale, 2))


def key(text: str, spec: VoiceSpec) -> str:
    raw = "|".join((VERSION, spec.voice, "%.2f" % spec.scale, EFFECT, text.strip()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def pack_phrases(raw: dict) -> List[dict]:
    """Все фразы пакета: у вызовов 112 — реплики заявителя, у ДДС — доклады старшего бригады."""
    out: List[dict] = []
    seen = set()

    def add(text: Optional[str], spec: VoiceSpec) -> None:
        text = (text or "").strip()
        if not text:
            return
        k = key(text, spec)
        if k not in seen:
            seen.add(k)
            out.append({"key": k, "text": text, "voice": spec.voice, "scale": spec.scale})

    if raw.get("mode") == "cards":
        for call in raw.get("calls", []):
            spec = caller_voice(call)
            for text in (call.get("dialogue") or {}).values():
                if isinstance(text, str):
                    add(text, spec)
            add(UNKNOWN, spec)
    else:
        for card in raw.get("cards", []):
            for rep in card.get("brigade", []):
                add(rep.get("text"), BRIGADE)
    return out


class SpeechCatalog:
    """Готовый звук на диске: что уже озвучено и по какому адресу его отдаёт сервер."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, k: str) -> Path:
        return self.dir / (k + ".wav")

    def url(self, text: str, spec: VoiceSpec) -> Optional[str]:
        k = key(text, spec)
        return URL_PREFIX + k + ".wav" if self.path(k).is_file() else None

    def caller_audio(self, call: dict, text: str) -> Optional[str]:
        return self.url(text, caller_voice(call))

    def brigade_audio(self, text: str) -> Optional[str]:
        return self.url(text, BRIGADE)

    def missing(self, phrases: Iterable[dict]) -> List[dict]:
        return [p for p in phrases if not self.path(p["key"]).is_file()]

    def ensure_ring(self) -> str:
        """Звонок телефона оператора 112 — делается без синтезатора, чистым Python."""
        from speech.dsp import ring_wav
        path = self.dir / RING
        if not path.is_file():
            ring_wav(path)
        return URL_PREFIX + RING


# ---------- текст для синтезатора ----------
ONES = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
         "семнадцать", "восемнадцать", "девятнадцать"]
TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
ORD_PREP = ["", "первом", "втором", "третьем", "четвёртом", "пятом", "шестом", "седьмом", "восьмом", "девятом",
            "десятом", "одиннадцатом", "двенадцатом", "тринадцатом", "четырнадцатом", "пятнадцатом", "шестнадцатом",
            "семнадцатом", "восемнадцатом", "девятнадцатом", "двадцатом"]
ABBR = [(r"\bул\.", "улица "), (r"\bд\.\s*(?=\d)", "дом "), (r"\bкв\.", "квартира "), (r"\bкорп\.", "корпус "),
        (r"\bстр\.", "строение "), (r"\bпод\.", "подъезд "), (r"\bэт\.", "этаж "), (r"\bст\.", "станция "),
        (r"\bкм\b\.?", "километр "), (r"\bа/м\b", "автомобиль"), (r"\bпр-т\b", "проспект"), (r"\bб-р\b", "бульвар"),
        (r"\bпр-д\b", "проезд")]
LETTERS = {"ДТП": "дэ тэ пэ", "УК": "у ка", "ГБУ": "гэ бэ у", "ОЭК": "о э ка", "ООО": "о о о", "МЖД": "эм жэ дэ",
           "ЦОДД": "цэ о дэ дэ", "ДДС": "дэ дэ эс", "МЧС": "эм чэ эс", "ГИБДД": "гэ и бэ дэ дэ"}
PHONE = re.compile(r"(?<![\d+])(?:(\+7|8)[\s\-(]*)?(\d{3})[\s\-)]*(\d{3})[\s\-]*(\d{2})[\s\-]*(\d{2})(?!\d)")
CONTEXT = [(r"\b(на|по) ул\.", r"\1 улице "), (r"\b(около|у|от|до) ст\.", r"\1 станции "),
           (r"\b(около|у|от|до) д\.\s*(?=\d)", r"\1 дома ")]


def number_words(n: int) -> str:
    if n < 10:
        return ONES[n]
    parts = []
    if n >= 100:
        parts.append(HUNDREDS[n // 100])
        n %= 100
    if 10 <= n < 20:
        parts.append(TEENS[n - 10])
    else:
        if n >= 20:
            parts.append(TENS[n // 10])
        if n % 10:
            parts.append(ONES[n % 10])
    return " ".join(parts)


def _group(digits: str) -> str:
    if len(digits) > 1 and digits[0] == "0":  # «05» — «ноль пять», как диктуют номер
        return " ".join(ONES[int(c)] for c in digits)
    return number_words(int(digits))


def _ordinal_floor(m: re.Match) -> str:
    n = int(m.group(1))
    if 1 <= n <= 20:
        return "на %s этаже" % ORD_PREP[n]
    if n < 30:
        return "на двадцать %s этаже" % ORD_PREP[n - 20]
    return m.group(0)


def split_for_synthesis(text: str, limit: int = 110) -> List[Tuple[str, float]]:
    """Части не длиннее limit символов и пауза после каждой. Память синтеза растёт с длиной фразы:
    реплика в 290 символов (27 с речи) одним куском требует ~1,2 ГБ, по частям — в разы меньше.
    Режем по предложениям, длинное предложение — по запятым и точкам с запятой, в крайнем случае по пробелам."""
    out: List[Tuple[str, float]] = []
    for sentence in re.findall(r"[^.!?…]+[.!?…]*", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        pieces = re.split(r"(?<=[,;:])\s+|\s+(?=\()|(?<=\))\s+", sentence) if len(sentence) > limit else [sentence]
        chunk = ""
        for piece in pieces:
            while len(piece) > limit:  # без знаков препинания — по последнему пробелу до границы
                cut = piece.rfind(" ", 0, limit)
                cut = cut if cut > limit // 3 else limit
                if chunk:
                    out.append((chunk, 0.12))
                    chunk = ""
                out.append((piece[:cut].strip(), 0.08))
                piece = piece[cut:].strip()
            if chunk and len(chunk) + 1 + len(piece) > limit:
                out.append((chunk, 0.12))
                chunk = piece
            else:
                chunk = (chunk + " " + piece).strip()
        if chunk:
            out.append((chunk, 0.12))
        out[-1] = (out[-1][0], 0.3)  # конец предложения — пауза длиннее
    return out


def speakable(text: str) -> str:
    """Текст, который синтезатор прочтёт как человек: телефоны по группам цифр, сокращения полностью,
    «на 5 этаже» — «на пятом этаже». Отображаемый текст реплики не меняется."""
    def phone(m: re.Match) -> str:
        head = "плюс семь, " if m.group(1) == "+7" else "восемь, " if m.group(1) == "8" else ""
        return head + ", ".join(_group(g) for g in m.groups()[1:])

    out = PHONE.sub(phone, text)
    out = re.sub(r"на (\d{1,2}) этаже", _ordinal_floor, out)
    for rx, repl in CONTEXT + ABBR:  # сначала сокращения с падежом по предлогу: «на ул.» — «на улице»
        out = re.sub(rx, repl, out)
    for abbr, spoken in LETTERS.items():
        out = re.sub(r"\b%s\b" % abbr, spoken, out)
    out = re.sub(r"\s*—\s*", ", ", out.replace("«", "").replace("»", ""))
    out = re.sub(r"\s+([,.;:!?)])", r"\1", re.sub(r"\s{2,}", " ", out))
    return out.strip()
