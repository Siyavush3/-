"""Второй слой маршрутизации: территориальные классы → конкретные ДДС по адресу (ARCHITECTURE.md §10.8).

Правило заказчика: «службы подтягиваются по принципу района обслуживания» — пожар в Щукино
уходит в ДДС управы Щукино и ДДС Северо-Западного округа. На стенде то же видно для ТиНАО:
«Поселение Вороновское» + «Поселение ТиНАО».

Классификатор отдаёт только классы («ОИВ района и округа», «ОИВ поселения ТиНАО»,
«Автодороги округа»); какие именно службы за ними стоят, решает адрес.
"""
import json
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from arm112.paths import data_dir

TINAO = "ТиНАО"
CLASS_DISTRICT = "ОИВ района и округа"
CLASS_TINAO = "ОИВ поселения ТиНАО"
CLASS_ROADS = "Автодороги округа"


@dataclass(frozen=True)
class Place:
    district: str   # район города или поселение ТиНАО: «Щукино», «Вороновское»
    okrug: str      # аббревиатура округа: «СЗАО», … , «ТиНАО»
    objects: Tuple[str, ...] = ()  # признаки объекта для слоя 3 («высотка ЭВАЖД», «Московская область» …)


class Territory:
    def __init__(self, services: Iterable[dict]):
        self._by_short: Dict[str, dict] = {s["short"]: s for s in services}

    @classmethod
    def load(cls, path=None) -> "Territory":
        with open(path or data_dir() / "services" / "services.json", encoding="utf-8") as f:
            return cls(json.load(f)["services"])

    def names(self) -> FrozenSet[str]:
        """Все службы справочника (краткие подписи плашек)."""
        return frozenset(self._by_short)

    def district_service(self, district: str) -> Optional[str]:
        """ДДС района или поселения: «Поселение Щукино», у части районов — «Упр. района …»."""
        for short in ("Поселение " + district, "Упр. района " + district):
            if short in self._by_short:
                return short
        return None

    def okrug_service(self, okrug: str) -> Optional[str]:
        short = "Поселение " + okrug  # ДДС префектуры: «Поселение СЗАО», «Поселение ТиНАО»
        return short if short in self._by_short else None

    def roads_service(self, okrug: str) -> Optional[str]:
        short = "ГБУ АД " + okrug
        return short if short in self._by_short else None

    def resolve(self, classes: Iterable[str], place: Optional[Place]) -> Tuple[str, ...]:
        """Конкретные службы для территориальных классов маршрута, по порядку плашек:
        ДДС префектуры, ДДС района (поселения), «Автодороги» округа — так на стенде (sdds_12).
        Без адреса — пусто."""
        if place is None:
            return ()
        classes = set(classes)
        in_tinao = place.okrug == TINAO
        out: List[str] = []
        if (CLASS_DISTRICT in classes and not in_tinao) or (CLASS_TINAO in classes and in_tinao):
            out += [self.okrug_service(place.okrug), self.district_service(place.district)]
        if CLASS_ROADS in classes:
            out.append(self.roads_service(place.okrug))
        return tuple(dict.fromkeys(s for s in out if s))
