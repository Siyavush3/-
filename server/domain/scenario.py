"""Пакет сценариев и маршрутизация карточки со всеми слоями (ARCHITECTURE.md §10.8, §18.4).

Пакет — данные (data/scenarios/*.json): карточки с эталоном ``expect`` и докладами бригады.
Здесь пакет проверяется целиком при загрузке, а карточка превращается в то,
что видит диспетчер ДДС: заголовок опросной карты, признаки, класс, полоса служб.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from arm112.paths import data_dir
from arm112.reference.fsm import ResponseFsm
from arm112.reference.routing import Classifier
from arm112.reference.territory import Place, Territory

FLAG_TEXT = {  # как признаки-флаги выглядят в строке признаков у ДДС («Есть угроза людям»)
    "УЛ": "Есть угроза людям", "НД": "Нет доступа", "ПП": "Есть пострадавшие",
    "ПРАВОНАРУШЕНИЕ": "Есть правонарушение", "ГАЗ": "Есть газификация", "МЕДПОМОЩЬ": "Нужна медпомощь",
    "ЭВАКУАЦИЯ": "Требуется эвакуация", "ПЕРЕКРЫТИЕ": "Перекрытие движения", "ТОННЕЛЬ": "Тоннель",
    "ПЕШ_ПЕРЕХОД": "Пешеходный переход",
}


OKRUG_TITLE = {  # вторая строка адреса у ДДС — полное название округа (sdds_12)
    "ЦАО": "Центральный административный округ", "САО": "Северный административный округ",
    "СВАО": "Северо-Восточный административный округ", "ВАО": "Восточный административный округ",
    "ЮВАО": "Юго-Восточный административный округ", "ЮАО": "Южный административный округ",
    "ЮЗАО": "Юго-Западный административный округ", "ЗАО": "Западный административный округ",
    "СЗАО": "Северо-Западный административный округ", "ЗелАО": "Зеленоградский административный округ",
    "НАО": "Новомосковский административный округ", "ТАО": "Троицкий административный округ",
}


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Plaque:
    name: str
    row: int              # 1 — службы по ЕКП, 2 — территориальные ДДС
    main: bool = False    # главная служба — подчёркнута
    manual: bool = False  # добавлена оператором 112 вручную — серая плашка

    def to_dict(self):
        return {"name": self.name, "row": self.row, "main": self.main, "manual": self.manual}


class Router:
    """Маршрут карточки: слой 1 — классификатор (C-ядро или эталон), слой 2 — адрес."""

    def __init__(self, classifier: Classifier, territory: Territory, ui: dict, ekp=None):
        self.classifier = classifier
        self.territory = territory
        self.ekp = ekp  # arm112.ekp.Ekp или None — тогда эталон
        self._main = ui.get("main_service_codes", {})
        self._order = {name: i for i, name in enumerate(ui.get("plaque_order", []))}
        self._object_dependent: Dict[str, str] = ui.get("object_dependent", {})

    @property
    def engine_name(self) -> str:
        return "libarm112 (C)" if self.ekp is not None else "эталон (Python)"

    def _sort(self, names: Iterable[str]) -> List[str]:
        return sorted(names, key=lambda n: (self._order.get(n, len(self._order)), n))

    def plaques(self, code: str, flags: Iterable[str], place: Optional[Place],
                manual: Iterable[str] = ()) -> List[Plaque]:
        """Плашки в порядке полосы служб. Полоса — одна последовательность с переносом:
        на стенде при 11 плашках 8 стоят внизу, 3 переносятся вверх (sdds_12)."""
        route = (self.ekp or self.classifier).route(code, flags)
        objects = set(place.objects) if place else set()
        services = {s for s in route.services
                    if s not in self._object_dependent or self._object_dependent[s] in objects}
        main = self._main.get(self.classifier.incidents[code]["main_service"])
        manual = [m for m in manual if m not in services]
        row1 = [Plaque(n, 1, main=(n == main), manual=(n in manual)) for n in self._sort(services | set(manual))]
        row2 = [Plaque(n, 2) for n in self.territory.resolve(route.territorial, place) if n not in manual]
        return row1 + row2


class Pack:
    def __init__(self, raw: dict, path: str = ""):
        self.raw = raw
        self.path = path
        self.id: str = raw["id"]
        self.title: str = raw["title"]
        self.role: str = raw["role"]
        self.role_title: str = raw.get("role_title", raw["role"])
        self.place = Place(raw["place"]["district"], raw["place"]["okrug"], tuple(raw["place"].get("objects", ())))
        self.reference: dict = raw.get("reference", {})
        self.cards: List[dict] = sorted(raw["cards"], key=lambda c: c["at"])
        self.by_key: Dict[str, dict] = {c["key"]: c for c in self.cards}

    @classmethod
    def load(cls, path) -> "Pack":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f), str(path))

    def validate(self, classifier: Classifier, territory: Territory, fsm: ResponseFsm, router: Router) -> None:
        """Проверка всего пакета при загрузке: сценарий с ошибкой не должен дойти до занятия."""
        def fail(card, msg):
            raise ScenarioError("%s, карточка %s: %s" % (self.id, card.get("key", "?"), msg))

        if len(self.by_key) != len(self.cards):
            raise ScenarioError("%s: повторяются ключи карточек" % self.id)
        services = territory.names()
        if self.role not in services:
            raise ScenarioError("%s: роль «%s» не найдена в справочнике служб" % (self.id, self.role))
        for card in self.cards:
            if card["code"] not in classifier.incidents:
                fail(card, "нет кода %s в классификаторе" % card["code"])
            unknown = set(card.get("flags", [])) - set(classifier.flags)
            if unknown:
                fail(card, "неизвестные флаги %s" % sorted(unknown))
            for name in card.get("manual_services", []):
                if name not in services:
                    fail(card, "служба «%s» не из справочника" % name)
            names = {p.name for p in router.plaques(card["code"], card.get("flags", []), self.place_for(card),
                                                     card.get("manual_services", []))}
            if self.role not in names:
                fail(card, "карточка не доходит до роли «%s» — проверьте код, адрес или ручное добавление" % self.role)
            expect = card["expect"]
            if expect["primary"] not in ("Принята", "Не принята"):
                fail(card, "первичный статус эталона — «Принята» или «Не принята»")
            current = "Принята"
            for rep in card.get("brigade", []):
                if rep["status"] not in fsm.allowed_next(current):
                    fail(card, "доклад «%s» ведёт в недопустимый после «%s» статус" % (rep["text"], current))
                current = rep["status"]
            if card.get("brigade") and expect.get("final") and current != expect["final"]:
                fail(card, "итог по докладам «%s» не совпадает с эталоном «%s»" % (current, expect["final"]))
            for group in expect.get("comment_facts", []) + expect.get("final_comment_facts", []):
                for v in group:
                    if v.startswith("{") and v[1:-1] not in self.by_key:
                        fail(card, "ссылка %s на несуществующую карточку" % v)

    def place_for(self, card: dict) -> Place:
        """Адрес карточки: район и округ пакета плюс признаки объекта из адреса карточки."""
        objects = tuple(self.place.objects) + tuple(card["address"].get("objects", ()))
        return Place(self.place.district, self.place.okrug, objects)

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "role": self.role, "role_title": self.role_title,
                "cards": len(self.cards), "duration_s": max(c["at"] for c in self.cards)}


class CardViews:
    """Как карточка выглядит у диспетчера ДДС (UI-SPEC.md §13.2)."""

    def __init__(self, classifier: Classifier, group_cards: dict):
        self.classifier = classifier
        self.group_cards = group_cards

    @classmethod
    def load(cls, classifier: Classifier) -> "CardViews":
        with open(data_dir() / "classifier" / "group_call_types.json", encoding="utf-8") as f:
            return cls(classifier, json.load(f)["groups"])

    def build(self, card: dict, number: str, saved_at: float, place: Place, plaques: List[Plaque]) -> dict:
        inc = self.classifier.incidents[card["code"]]
        group = self.group_cards.get(inc["group"], {"list": inc["group"], "title": inc["group"]})
        attrs = [a for a in (inc["attr1"], inc["attr2"], inc["attr3"]) if a]
        features = " . ".join(attrs + [FLAG_TEXT[f] for f in card.get("flags", []) if f in FLAG_TEXT]) + " ."
        addr = card["address"]
        line = "Россия, Москва, (%s, %s), %s, %s" % (place.okrug, place.district, addr["street"], addr["house"])
        if addr.get("entrance"):
            line += ", под. %s" % addr["entrance"]
        flags = set(card.get("flags", []))
        return {
            "number": number,
            "saved_at": saved_at,
            # Опер./АРМ — оператор Службы 112, создавший карточку; в учебном контуре это бот
            "op_no": "1",
            "arm_no": "1",
            "operator": "Опер. 1, АРМ 1, Оператор 112 (бот)",
            "author": "1 Оператор 112",
            "caller": card["caller"],
            "address": line,
            "address_short": "Москва , (%s, %s) , %s, %s" % (place.okrug, place.district, addr["street"], addr["house"]),
            "description": card["description"],
            "card_title": group["title"],
            "list_type": group["list"],
            "features": features,
            "class": inc["type"],
            "okrug_title": OKRUG_TITLE.get(place.okrug, place.okrug),
            "victims": "есть" if "ПП" in flags else "нет",
            "blocked": "да" if "НД" in flags else "нет",
            "chp": bool(card.get("chp")),  # отметка «ЧП» оператора 112 (на стенде у пожара активна)
            "plaques": [p.to_dict() for p in plaques],
        }
