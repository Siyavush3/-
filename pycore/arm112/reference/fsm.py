"""Эталонный автомат статусов реагирования службы (ARCHITECTURE.md §10.2, §8.4).

Проверяет трек статусов одной службы по одной карточке: допустимость переходов,
обязательность комментария, норматив на первичный статус. Время — секунды
по часам сервера (ARCHITECTURE.md §18.5).
"""
import json
from dataclasses import dataclass
from typing import List, Optional, Sequence

from arm112.paths import data_dir


@dataclass(frozen=True)
class Step:
    status: str
    at: float          # секунды, серверные часы
    comment: str = ""


@dataclass(frozen=True)
class Violation:
    kind: str          # «нет статуса» | «норматив» | «переход» | «комментарий» | «после завершения»
    status: str
    detail: str


class ResponseFsm:
    def __init__(self, table: dict):
        self.system = set(table["system_statuses"])
        self.initial: List[str] = list(table["initial"]["next"])
        self.transitions = {k: list(v["next"]) for k, v in table["transitions"].items()}
        self.terminal = set(table["terminal"])
        self.comment_required = set(table["comment_required"])
        self.primary_norm_s: float = float(table["primary_status_norm_s"])

    @classmethod
    def load(cls, path=None) -> "ResponseFsm":
        with open(path or data_dir() / "fsm" / "response_status.json", encoding="utf-8") as f:
            return cls(json.load(f))

    def allowed_next(self, current: Optional[str]) -> List[str]:
        """Что предложит выпадающий список после статуса current (None — ещё ничего не проставлено)."""
        if current is None or current in self.system:
            return list(self.initial)
        if current in self.terminal:
            return []
        return list(self.transitions.get(current, []))

    def check(self, track: Sequence[Step]) -> List[Violation]:
        """Нарушения в треке. Первым шагом ожидается системный «Добавлена» — от него идёт отсчёт норматива."""
        violations: List[Violation] = []
        added_at = next((s.at for s in track if s.status == "Добавлена"), None)
        current: Optional[str] = None
        for step in track:
            if step.status in self.system:
                continue
            if current in self.terminal:
                violations.append(Violation("после завершения", step.status,
                                            "карточка закрыта статусом «%s»" % current))
                continue
            if step.status not in self.allowed_next(current):
                violations.append(Violation("переход", step.status,
                                            "после «%s» доступно: %s" % (current or "—", ", ".join(self.allowed_next(current)))))
            if current is None and added_at is not None and step.at - added_at > self.primary_norm_s:
                violations.append(Violation("норматив", step.status, "%.0f с при нормативе %.0f с"
                                            % (step.at - added_at, self.primary_norm_s)))
            if step.status in self.comment_required and not step.comment.strip():
                violations.append(Violation("комментарий", step.status, "комментарий обязателен"))
            current = step.status
        if current is None:
            violations.append(Violation("нет статуса", "—", "служба не проставила ни одного статуса"))
        return violations
