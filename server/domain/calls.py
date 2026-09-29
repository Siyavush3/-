"""Режим «карточки»: оператор Службы 112 принимает вызов и заполняет карточку (ТЗ, сценарий «карточки»).

Здесь всё, что не зависит от транспорта и часов движка:
  - CallPack    — пакет вызовов с эталоном карточки (data/scenarios/calls_*.json), проверяется при загрузке;
  - Caller      — заявитель-бот: отвечает на вопросы оператора по фактам вызова;
  - Questionnaire — опросная карта группы классификатора: каскад признаков и вопросы «да/нет»;
  - evaluate_card — сравнение заполненной карточки с эталоном, замечания по категориям rubric_112.json.
Ход занятия (звонки по очереди, сохранение карточек) — domain.engine.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

from arm112.paths import data_dir
from arm112.reference.routing import Classifier
from domain.evaluation import Finding

STATUSES = ["очевидец", "пострадавший", "родственник", "знакомый", "ребенок", "участник"]
SUBJECTS = ["Москва", "Московская область", "другой регион"]

# Признаки-переключатели в верхней строке карточки (UI-SPEC §4.1) — всегда на экране
TOP_FLAGS = {"НД": "Нет доступа / заблокированные", "ПП_НЕ_НА_МЕСТЕ": "Нет на месте / отказ от скорой"}
# Вопросы «да/нет» опросной карты — это условия маршрутизации классификатора (UI-SPEC §10)
FLAG_QUESTIONS = {
    "УЛ": "Угроза людям", "ГАЗ": "Проведена ли газификация", "ПРАВОНАРУШЕНИЕ": "Есть правонарушение",
    "МЕДПОМОЩЬ": "Нужна медицинская помощь", "ЭВАКУАЦИЯ": "Требуется эвакуация",
    "БОЛЕЕ_5_ЧЕЛ": "Пострадавших больше пяти", "ПЕРЕКРЫТИЕ": "Есть ли перекрытие движения",
    "ТОННЕЛЬ": "Место происшествия: тоннель", "ПЕШ_ПЕРЕХОД": "Место происшествия: пешеходный переход",
    "АВТО_ОБЪЕКТ": "Объект автотранспорта", "ОБЪЕКТ_СВЯЗИ": "Объект связи", "СТРОЙКА": "Стройка",
    "ОБЪЕКТ_ИЗ_ПЕРЕЧНЯ": "Объект из перечня",
}
FLAG_TEXT = {"ПП": "есть пострадавшие", "НД": "нет доступа", "ПП_НЕ_НА_МЕСТЕ": "пострадавших нет на месте",
             **{k: v.lower() for k, v in FLAG_QUESTIONS.items()}}

ADDRESS_PARTS = {"korpus": "корпус", "stroenie": "строение", "flat": "квартира", "entrance": "подъезд",
                 "floor": "этаж", "code": "код домофона"}
STREET_WORDS = {"улица", "ул", "проспект", "пр", "пр-т", "просп", "переулок", "пер", "проезд", "пр-д", "бульвар",
                "б-р", "бул", "набережная", "наб", "шоссе", "ш", "площадь", "пл", "тупик", "аллея", "мкр"}

# Намерения вопросов оператора: кнопки шлют намерение, свободный текст и распознанная речь разбираются по
# основам слов (распознавание путает окончания и теряет короткие слова: «что случилось» → «то случилось»).
# Порядок важен: «Кем вы приходитесь пострадавшему?» — вопрос о статусе, а не о пострадавших
# («приходит в сознание» — о пострадавших: поэтому формы «приходитесь», а не основа «приход»).
INTENTS = [
    ("address_clarified", ["уточн", "точный адрес", "номер дома", "ориентир"]),
    ("address", ["адрес", "где ", "где?", "куда", "место", "находит"]),
    ("name", ["зовут", "фамил", "имя", "фио", "представ"]),
    ("phone", ["телефон", "номер для связ", "перезвон", "связаться"]),
    ("status", ["кем", "кто вы", "приходитесь", "приходится", "приходились", "приходите с", "родствен", "очевид",
                "свидетел"]),
    ("victims", ["пострада", "ранен", "травм", "жертв", "дышит", "сознан", "живы"]),
    ("danger", ["угроз", "опасн"]),
    ("gas", ["газ"]),
    ("access", ["доступ", "проехать", "подъехать", "пройти"]),
    ("details", ["подробн", "что именно", "расскаж", "опиши", "случил", "произош", "слуша"]),
]


def norm(text: Optional[str]) -> str:
    return re.sub(r"[^\w]+", " ", (text or "").lower().replace("ё", "е")).strip()


def street_core(text: Optional[str]) -> str:
    return " ".join(w for w in norm(text).split() if w not in STREET_WORDS)


def compact(text: Optional[str]) -> str:
    return norm(text).replace(" ", "")


def detect_intent(text: str) -> Optional[str]:
    t = " " + (text or "").lower().replace("ё", "е") + " "
    for intent, words in INTENTS:
        if any(w in t for w in words):
            return intent
    return None


class ScenarioErrorCalls(ValueError):
    pass


class CallPack:
    mode = "cards"

    def __init__(self, raw: dict, path: str = ""):
        self.raw = raw
        self.path = path
        self.id: str = raw["id"]
        self.title: str = raw["title"]
        self.norm_s: float = float(raw.get("norm_s", 80))
        self.answer_norm_s: float = float(raw.get("answer_norm_s", 15))
        self.calls: List[dict] = raw["calls"]
        self.by_key: Dict[str, dict] = {c["key"]: c for c in self.calls}
        self.role = None
        self.role_title = "Оператор Службы 112"
        self.reference: dict = raw.get("reference", {})

    @classmethod
    def load(cls, path) -> "CallPack":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f), str(path))

    def validate(self, classifier: Classifier) -> None:
        if len(self.by_key) != len(self.calls):
            raise ScenarioErrorCalls("%s: повторяются ключи вызовов" % self.id)
        for c in self.calls:
            gold = c["gold"]
            for code in gold["codes"]:
                inc = classifier.incidents.get(code)
                if inc is None or not inc["operator_visible"]:
                    raise ScenarioErrorCalls("%s, вызов %s: код %s не виден оператору" % (self.id, c["key"], code))
            if set(gold["flags"]) - set(classifier.flags):
                raise ScenarioErrorCalls("%s, вызов %s: неизвестные признаки" % (self.id, c["key"]))
            if gold["statuses"] and set(gold["statuses"]) - set(STATUSES):
                raise ScenarioErrorCalls("%s, вызов %s: неизвестный статус заявителя" % (self.id, c["key"]))
            if gold["address"].get("subject") not in (None, *SUBJECTS):
                raise ScenarioErrorCalls("%s, вызов %s: неизвестный субъект" % (self.id, c["key"]))
            if not c["dialogue"].get("opening"):
                raise ScenarioErrorCalls("%s, вызов %s: нет первой реплики заявителя" % (self.id, c["key"]))

    def groups(self, classifier: Classifier) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for c in self.calls:
            g = classifier.incidents[c["gold"]["codes"][0]]["group"]
            out[g] = out.get(g, 0) + 1
        return out

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "mode": "cards", "role": None, "role_title": self.role_title,
                "cards": len(self.calls), "duration_s": 0, "norm_s": self.norm_s}


class Caller:
    """Заявитель-бот. Отвечает только тем, что есть в вызове; на повторный вопрос об адресе — уточняет."""

    UNKNOWN = "Не понимаю вас. Повторите, пожалуйста."

    def __init__(self, call: dict):
        self.call = call
        self.asked: List[str] = []

    def answer(self, intent: Optional[str]) -> str:
        d = self.call["dialogue"]
        if intent is None:
            return self.UNKNOWN
        if intent == "address" and "address" in self.asked:
            intent = "address_clarified"
        self.asked.append(intent)
        return d.get(intent) or self.UNKNOWN


@dataclass
class Questionnaire:
    """Опросная карта группы: каскад признаков (п1 → п2 → п3) и вопросы «да/нет»."""
    group: str
    title: str
    incidents: List[dict]
    flags: List[str]

    def to_dict(self) -> dict:
        return {"group": self.group, "title": self.title, "incidents": self.incidents,
                "questions": [{"flag": f, "text": FLAG_QUESTIONS[f]} for f in self.flags if f in FLAG_QUESTIONS],
                "top": [{"flag": f, "text": t} for f, t in TOP_FLAGS.items()]}


def group_flags(classifier: Classifier, group: str) -> List[str]:
    """Признаки, от которых зависит маршрутизация хотя бы одного кода группы."""
    used = set()
    for inc in classifier.incidents.values():
        if inc["group"] != group:
            continue
        for r in inc.get("routing", []):
            cond = r.get("cond") or {}
            used.update(cond.get("all_of", []))
            used.update(cond.get("none_of", []))
    return sorted(used)


def questionnaire(classifier: Classifier, group: str, title: str) -> Questionnaire:
    rows = [{"code": i["code"], "attr1": i["attr1"], "attr2": i["attr2"], "attr3": i["attr3"], "type": i["type"]}
            for i in classifier.incidents.values() if i["group"] == group and i["operator_visible"]]
    return Questionnaire(group, title, rows, group_flags(classifier, group))


# ---------- оценка карточки ----------
@dataclass
class CardTiming:
    rang_at: float
    answered_at: Optional[float]
    saved_at: Optional[float]


def _missing_facts(text: str, groups: Sequence[Sequence[str]]) -> List[List[str]]:
    t = norm(text)
    return [list(g) for g in groups if not any(norm(v) in t for v in g)]


def evaluate_card(call: dict, card: dict, timing: CardTiming, classifier: Classifier, rubric: dict,
                  auto_services: Iterable[str], norm_s: float, answer_norm_s: float,
                  spelling: Optional[List[dict]] = None, checker=None) -> List[Finding]:
    """Замечания к одной карточке. card — то, что прислал обучающийся (см. web/js/op112.js)."""
    gold = call["gold"]
    number = card.get("number")
    out: List[Finding] = []

    def add(category: int, detail: str) -> None:
        out.append(Finding(category, detail, card=number))

    # тип происшествия
    code = card.get("code")
    gold_inc = classifier.incidents[gold["codes"][0]]
    if code not in gold["codes"]:
        chosen = classifier.incidents.get(code) if code else None
        if chosen and chosen["group"] == gold_inc["group"]:
            add(2, "выбрано «%s», а по вызову — «%s»" % (chosen["type"], gold_inc["type"]))
        else:
            add(1, "выбрано «%s», а по вызову — «%s»" % (chosen["type"] if chosen else card.get("call_type") or "ничего",
                                                         gold_inc["type"]))

    # признаки: только те, что опросная карта этой группы вообще спрашивает
    asked = set(group_flags(classifier, gold_inc["group"])) | {"ПП", "НД", "ПП_НЕ_НА_МЕСТЕ"}
    flags = set(card.get("flags") or [])
    victims = card.get("victims")
    if victims is True:
        flags.add("ПП")
    missing = [f for f in gold["flags"] if f in asked and f not in flags and f != "ПП"]
    if missing:
        add(3, "не отмечено: " + ", ".join(FLAG_TEXT.get(f, f) for f in missing))
    if gold.get("victims") is True and victims is False:
        add(3, "пострадавшие есть, а в карточке — «нет»")
    elif gold.get("victims") is False and victims is True:
        add(3, "пострадавших нет, а в карточке — «есть»")

    # обязательные поля (инструкция, раздел 5)
    caller = card.get("caller") or {}
    empty = [title for key, title in (("name", "ФИО заявителя"), ("status", "статус заявителя")) if not (caller.get(key) or "").strip()]
    if victims is None:
        empty.append("пострадавшие")
    if not (card.get("description") or "").strip():
        empty.append("описание")
    if empty:
        add(6, "не заполнено: " + ", ".join(empty))
    if gold.get("name") and (caller.get("name") or "").strip():
        surname = norm(gold["name"]).split()[0]
        if surname not in norm(caller["name"]).split():
            add(6, "ФИО заявителя не совпадает с названным: %s" % gold["name"])
    if gold.get("statuses") and caller.get("status") and caller["status"] not in gold["statuses"]:
        add(6, "статус заявителя «%s», по вызову — «%s»" % (caller["status"], gold["statuses"][0]))

    # адрес
    addr = card.get("address") or {}
    gaddr = gold["address"]
    if gaddr.get("subject") and addr.get("subject") != gaddr["subject"]:
        add(4, "субъект «%s», а вызов — «%s»" % (addr.get("subject") or "не указан", gaddr["subject"]))
    if gaddr.get("street"):
        want, got = street_core(gaddr["street"]), street_core(addr.get("street"))
        if not got or (want not in got and got not in want):
            add(4, "улица: указано «%s», верно — «%s»" % (addr.get("street") or "—", gaddr["street"]))
    if gaddr.get("house") and compact(addr.get("house")) != compact(gaddr["house"]):
        add(4, "дом: указано «%s», верно — «%s»" % (addr.get("house") or "—", gaddr["house"]))
    if gaddr.get("kw"):
        blob = norm(" ".join(str(addr.get(k) or "") for k in ("locality", "street", "house", "descriptive")))
        if not any(norm(k) in blob for k in gaddr["kw"]):
            add(4, "в адресе нет ориентира из вызова: %s" % ", ".join(gaddr["kw"][:2]))
    if not any((addr.get(k) or "").strip() for k in ("street", "house", "descriptive", "locality")):
        add(4, "адрес не заполнен")
    incomplete = [title for key, title in ADDRESS_PARTS.items()
                  if gaddr.get(key) and compact(addr.get(key)) != compact(gaddr[key])]
    if incomplete:
        add(5, "не указано или неверно: " + ", ".join(incomplete))

    # описание
    description = card.get("description") or ""
    lost = checker.missing(description, gold["facts"]) if checker else _missing_facts(description, gold["facts"])
    if description.strip() and lost:
        add(7, "не упомянуто: " + "; ".join(" / ".join(g[:3]) for g in lost))
    services = set(auto_services)
    first = rubric.get("first_chars_03", 100)
    head = (checker.missing if checker else _missing_facts)(description[:first], gold["facts"][:1]) if gold["facts"] else []
    if "Служба 103" in services and description.strip() and head:
        add(7, "в первых %d символах описания (уходят в службу 03) нет главного: %s"
            % (first, " / ".join(gold["facts"][0][:3])))
    if len(description) > rubric.get("description_limit", 1999):
        add(7, "описание длиннее %d символов" % rubric.get("description_limit", 1999))
    if checker is not None and spelling is None:
        spelling = checker.spelling(description)
        for err in checker.grammar(description):
            add(8, "%s: «%s»" % (err["detail"], err["fragment"]))
    for err in spelling or []:
        add(8, "«%s» → %s" % (err["word"], ", ".join(err.get("suggestions") or ["нет в словаре"])))

    # время
    if timing.answered_at is not None and timing.rang_at is not None and timing.answered_at - timing.rang_at > answer_norm_s:
        add(11, "вызов принят через %.0f с при нормативе %.0f с" % (timing.answered_at - timing.rang_at, answer_norm_s))
    if timing.answered_at is not None and timing.saved_at is not None and timing.saved_at - timing.answered_at > norm_s:
        add(9, "карточка заполнена за %.0f с при нормативе %.0f с" % (timing.saved_at - timing.answered_at, norm_s))

    # службы
    added, removed = card.get("services_added") or [], card.get("services_removed") or []
    if added or removed:
        parts = (["добавлено: " + ", ".join(added)] if added else []) + (["убрано: " + ", ".join(removed)] if removed else [])
        add(10, "; ".join(parts))
    return out


def card_score(findings: Sequence[Finding], rubric: dict) -> int:
    weights = {int(k): v["weight"] for k, v in rubric["categories"].items()}
    cap = {8: 3}  # орфография: не больше трёх штрафов за карточку
    counted: Dict[int, int] = {}
    total = 0
    for f in findings:
        counted[f.category] = counted.get(f.category, 0) + 1
        if f.category in cap and counted[f.category] > cap[f.category]:
            continue
        total += weights[f.category]
    return max(0, 100 - total)


def load_rubric112(path=None) -> dict:
    with open(path or data_dir() / "evaluation" / "rubric_112.json", encoding="utf-8") as f:
        return json.load(f)
