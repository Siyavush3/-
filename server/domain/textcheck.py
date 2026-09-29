"""Проверка текста, введённого обучающимся (узел 3).

ТЗ: «оценка уровня подготовки, в том числе на основе анализа ручного ввода текста», в отчёте занятия —
«грамматика». Проверка детерминированная и объяснимая, без нейросетей (ARCHITECTURE.md §1):

- орфография — по словарю русской морфологии (pymorphy3, словарь OpenCorpora): слово, которого нет
  в словаре ни в одной форме, — ошибка; варианты исправления — известные слова на расстоянии одной
  правки (пропуск, лишняя, заменённая или переставленная буква). Слово, известное словарю только как
  имя собственное, но написанное строчными («гарит» — форма имени «Гарита»), тоже считается ошибкой;
- оформление — правила: два пробела, пробел перед знаком препинания, нет пробела после запятой,
  латиница внутри русского слова, слово повторено подряд;
- факты — с учётом словоформ: «передали», «передана», «передано» сводятся к «передать».

Без пакета pymorphy3 проверка отключается (факты сравниваются по вхождению строки, как раньше).
"""
from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set

WORD = re.compile(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*")
RU = "абвгдежзийклмнопрстуфхцчшщъыьэюя"
VOWELS = set("аеиоуыэюя")
PROPER = {"Name", "Surn", "Patr", "Geox", "Orgn", "Trad"}
GRAMMAR_RULES = [
    ("double_space", re.compile(r"(?<=\S) {2,}(?=\S)"), "два пробела подряд"),
    ("space_before_punct", re.compile(r"\s+(?=[,.;:!?])"), "пробел перед знаком препинания"),
    ("no_space_after_comma", re.compile(r"[,;:](?=[А-Яа-яЁёA-Za-z])"), "нет пробела после знака препинания"),
    ("mixed_alphabet", re.compile(r"\b(?=\w*[A-Za-z])(?=\w*[А-Яа-яЁё])\w+\b"), "в слове смешаны латиница и кириллица"),
    ("repeated_word", re.compile(r"\b(\w{2,}(?:\s+\w{2,})?)\s+\1\b", re.IGNORECASE), "слова повторены подряд"),
]


def norm(text: Optional[str]) -> str:
    return (text or "").lower().replace("ё", "е")


def available() -> bool:
    try:
        import pymorphy3  # noqa: F401
        return True
    except ImportError:
        return False


class TextChecker:
    def __init__(self, extra_words: Iterable[str] = ()):
        import pymorphy3
        self.morph = pymorphy3.MorphAnalyzer()
        self.extra: Set[str] = {norm(w) for w in extra_words if w}
        self._known = functools.lru_cache(maxsize=100_000)(self._known_uncached)

    # ---------- словарь ----------
    def _known_uncached(self, word: str) -> int:
        """0 — неизвестно, 1 — известно, 2 — известно только как имя собственное."""
        w = norm(word)
        if w in self.extra:
            return 1
        if "-" in w:
            return 1 if all(self._known(p) for p in w.split("-") if p) else 0
        if not self.morph.word_is_known(w):
            return 0
        if all(PROPER & set(p.tag.grammemes) for p in self.morph.parse(w)):
            return 2
        return 1

    def known(self, word: str, lowercase: bool = True) -> bool:
        state = self._known(norm(word))
        return state == 1 or (state == 2 and not lowercase)

    def suggestions(self, word: str, limit: int = 3) -> List[str]:
        """Известные слова на расстоянии одной правки, самые правдоподобные опечатки — первыми.

        Частот слов в словаре нет, поэтому порядок задаёт тип правки: в русском чаще всего путают
        безударные гласные (а/о, е/и, е/я), затем пропускают или вставляют букву, реже переставляют
        буквы и заменяют согласные."""
        w = norm(word)
        cost = {}

        def offer(candidate: str, weight: int) -> None:
            if candidate != w and len(candidate) > 1 and weight < cost.get(candidate, 99):
                cost[candidate] = weight

        for i in range(len(w) + 1):
            a, b = w[:i], w[i:]
            if b:
                offer(a + b[1:], 2)                                          # лишняя буква
                for c in RU:
                    offer(a + c + b[1:], 1 if b[0] in VOWELS and c in VOWELS else 3)  # заменённая буква
            if len(b) > 1:
                offer(a + b[1] + b[0] + b[2:], 2)                            # переставленные буквы
            for c in RU:
                offer(a + c + b, 2)                                          # пропущенная буква
        found = [e for e in cost if self._known(e) == 1]
        found.sort(key=lambda e: (cost[e], e[0] != w[0], e))
        return found[:limit]

    # ---------- проверки ----------
    def spelling(self, text: str) -> List[dict]:
        issues, seen = [], set()
        for m in WORD.finditer(text or ""):
            token = m.group(0)
            if len(token) < 3 or (token.isupper() and len(token) <= 6):
                continue  # предлоги и аббревиатуры
            if not token.isupper() and any(ch.isupper() for ch in token[1:]):
                continue  # «ЛифтСервис», «МосГаз»: заглавная внутри слова — название организации
            start = m.start()
            prefix = text[:start].rstrip()
            sentence_start = not prefix or prefix[-1] in ".!?\n«\"("
            capital = token[0].isupper()
            if capital and not sentence_start:
                continue  # имена, улицы, названия организаций
            after = WORD.search(text, m.end())
            name_like = capital and after is not None and after.group(0)[0].isupper() \
                and not text[m.end():after.start()].strip(" ,")
            if name_like:
                continue  # «Липатина Ольга…»: в начале предложения идёт имя, а не опечатка
            if self.known(token, lowercase=True):
                continue
            if capital and self._known(norm(token)) == 2 and any(
                    "nomn" in x.tag.grammemes and PROPER & set(x.tag.grammemes) for x in self.morph.parse(token)):
                continue  # «Москва, …» в начале предложения; но «Гарит» (род. мн. имени «Гарита») — опечатка
            key = norm(token)
            if key in seen:
                continue
            seen.add(key)
            issues.append({"word": token, "start": start, "end": m.end(), "suggestions": self.suggestions(token)})
        return issues

    @staticmethod
    def grammar(text: str) -> List[dict]:
        issues = []
        for rule, rx, title in GRAMMAR_RULES:
            for m in rx.finditer(text or ""):
                issues.append({"rule": rule, "start": m.start(), "end": m.end(), "detail": title,
                               "fragment": (text[max(0, m.start() - 12):m.end() + 12]).strip()})
        return sorted(issues, key=lambda x: x["start"])

    def check(self, text: str) -> dict:
        return {"spelling": self.spelling(text), "grammar": self.grammar(text)}

    # ---------- факты ----------
    def lemmas(self, text: str) -> Set[str]:
        out = set()
        for token in WORD.findall(text or ""):
            t = norm(token)
            out.add(t)
            out.add(norm(self.morph.parse(t)[0].normal_form))
        return out

    def variant_lemma(self, variant: str) -> str:
        v = norm(variant).strip()
        return norm(self.morph.parse(v)[0].normal_form) if WORD.fullmatch(v) else v

    def missing(self, text: str, groups: Sequence[Sequence[str]]) -> List[List[str]]:
        """Группы фактов, которых нет в тексте. В группе достаточно одного варианта: как подстроки
        («передан» ⊂ «передана») или как словоформы («передали» → «передать»)."""
        low = norm(text)
        lem = self.lemmas(text)
        out = []
        for group in groups:
            ok = False
            for v in group:
                nv = norm(v)
                if nv in low or self.variant_lemma(v) in lem or any(l.startswith(nv) for l in lem if len(nv) >= 4):
                    ok = True
                    break
            if not ok:
                out.append(list(group))
        return out


def domain_words(classifier=None, packs: Iterable = (), services: Iterable[str] = (), path=None) -> Set[str]:
    """Слова, которые не надо считать ошибками: термины классификатора, названия служб, улицы из сценариев."""
    words: Set[str] = set()
    base = Path(path) if path else None
    if base is None:
        from arm112.paths import data_dir
        base = data_dir() / "text" / "domain_words.txt"
    if base.is_file():
        for line in base.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            words.update(WORD.findall(line))
    if classifier is not None:
        for inc in classifier.incidents.values():
            for field in ("type", "attr1", "attr2", "attr3"):
                words.update(WORD.findall(inc.get(field) or ""))
    for name in services:
        words.update(WORD.findall(name))
    for pack in packs:
        raw = getattr(pack, "raw", {})
        for card in raw.get("cards", []) + raw.get("calls", []):
            addr = card.get("address") or (card.get("gold") or {}).get("address") or {}
            words.update(WORD.findall(" ".join(str(v) for v in addr.values() if isinstance(v, str))))
        for section in (raw.get("reference") or {}).get("sections", []):
            for item in section.get("items", []):
                words.update(WORD.findall(item))
    return words
