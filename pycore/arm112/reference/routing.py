"""Эталонная маршрутизация по единому классификатору происшествий (ARCHITECTURE.md §10.8).

Правило, восстановленное по классификатору и сверенное с боевым стендом:
служба получает карточку, если хотя бы в одной её колонке для данного кода стоит
непустое значение, отличное от «нет реагирования», и условие колонки выполнено
на выбранных флагах опросной карты.

Что видно на АРМ-112, задаёт data/classifier/ui_services.json:
  - visible          — служба отображается плашкой в полосе служб;
  - channel_visible  — видна только колонка определённого канала (у «Мос.Без.» — АРМ-112);
  - territorial      — не служба, а класс территориальных получателей: конкретные ДДС
                       района, округа, поселения определяются по адресу (слой 2);
  - всё остальное    — информационные получатели, на АРМ-112 не отображаются.

C-модуль ekp обязан давать ровно тот же результат (tests/test_ekp_native.py).
"""

import json
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional

from arm112.paths import data_dir

NO_RESPONSE = "нет реагирования"


@dataclass(frozen=True)
class Route:
    services: FrozenSet[str]      # подписи плашек в полосе служб
    territorial: FrozenSet[str]   # классы территориальных получателей


def cond_ok(cond: Optional[dict], flags: FrozenSet[str]) -> bool:
    if not cond:
        return True
    if "all_of" in cond:
        return all(f in flags for f in cond["all_of"])
    if "none_of" in cond:
        return not any(f in flags for f in cond["none_of"])
    raise ValueError("нераспознанное условие: %r" % (cond,))


class Classifier:
    """Классификатор + правила отображения. Загружается один раз, дальше только чтение."""

    def __init__(self, classifier: dict, ui: dict):
        self.version: str = classifier.get("version", "?")
        self.flags: List[str] = list(classifier["flags"])
        self.incidents: Dict[str, dict] = {i["code"]: i for i in classifier["incidents"]}
        self._visible: Dict[str, str] = dict(ui["visible"])
        self._channel: Dict[tuple, str] = {(c["service"], c["channel"]): c["ui"]
                                           for c in ui["channel_visible"]}
        self._territorial: Dict[str, str] = dict(ui["territorial"])

    @classmethod
    def load(cls, classifier_path=None, ui_path=None) -> "Classifier":
        base = data_dir() / "classifier"
        with open(classifier_path or base / "classifier.json", encoding="utf-8") as f:
            classifier = json.load(f)
        with open(ui_path or base / "ui_services.json", encoding="utf-8") as f:
            ui = json.load(f)
        return cls(classifier, ui)

    def target(self, entry: dict):
        """Куда ведёт колонка: ('service', подпись) | ('territorial', класс) | None — не отображается."""
        service = entry["service"]
        if service in self._territorial:
            return "territorial", self._territorial[service]
        channel = entry.get("channel")
        if channel:
            name = self._channel.get((service, channel))
        else:
            name = self._visible.get(service)
        return ("service", name) if name else None

    def route(self, code: str, flags: Iterable[str]) -> Route:
        flags = frozenset(flags)
        unknown = flags - set(self.flags)
        if unknown:
            raise ValueError("неизвестные флаги: %s" % sorted(unknown))
        incident = self.incidents[code]  # KeyError — неизвестный код
        services, territorial = set(), set()
        for entry in incident["routing"]:
            if entry["value"] == NO_RESPONSE:
                continue
            target = self.target(entry)
            if target is None or not cond_ok(entry.get("cond"), flags):
                continue
            kind, name = target
            (services if kind == "service" else territorial).add(name)
        return Route(frozenset(services), frozenset(territorial))
