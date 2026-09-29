"""Занятие как локальное лобби (ARCHITECTURE.md §18).

Сервер — единственный арбитр: переходы состояний и отметки времени делаются здесь,
клиент только отображает. Модель не зависит от транспорта и хранилища —
их подключает прикладной слой. Часы подставляются снаружи (серверные).
"""
from __future__ import annotations

import dataclasses
import enum
import time
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Iterable, Optional

RECONNECT_GRACE_S = 30.0  # ТЗ: сбои сети до 30 с — восстановление без потери данных

OPERATOR_112 = "оператор 112"
DDS = "ДДС"
BOT_CALLER = "заявитель"
BOT_BRIGADE = "старший бригады"


class LessonError(Exception):
    pass


class InvalidTransition(LessonError):
    pass


class InvalidRole(LessonError):
    pass


class PermissionDenied(LessonError):
    pass


class State(enum.Enum):
    DRAFT = "черновик"
    RECRUITING = "набор"
    RUNNING = "идёт"
    FINISHED = "завершено"
    REPORTED = "отчёт"


class Mode(enum.Enum):
    CARDS = "карточки"                        # люди — операторы 112
    CARD_ACTIONS = "действия с карточками"    # люди — ДДС
    COMBINED = "совмещённое"                  # люди и там, и там (расширение, §18.3)


class CardSource(enum.Enum):  # формулировки ТЗ
    GENERATED = "сгенерированные системой"
    STUDENT = "сформированные обучающимися"
    MIXED = "смешанные"


class ActorKind(enum.Enum):
    TEACHER = "преподаватель"
    ADMIN = "администратор"
    STUDENT = "обучающийся"


class Connection(enum.Enum):
    ONLINE = "на связи"
    RECONNECTING = "восстанавливается"
    OFFLINE = "нет связи"


@dataclass(frozen=True)
class Actor:
    user_id: str
    kind: ActorKind


@dataclass(frozen=True)
class Role:
    kind: str                      # OPERATOR_112 | DDS
    service: Optional[str] = None  # для ДДС — подпись службы из справочника

    @staticmethod
    def operator_112() -> "Role":
        return Role(OPERATOR_112)

    @staticmethod
    def dds(service: str) -> "Role":
        return Role(DDS, service)


@dataclass
class Seat:
    participant_id: str
    role: Optional[Role] = None
    connected: bool = True
    disconnected_at: Optional[float] = None


@dataclass(frozen=True)
class Config:
    mode: Mode = Mode.CARD_ACTIONS
    categories: FrozenSet[str] = frozenset()
    card_source: CardSource = CardSource.GENERATED
    primary_status_norm_s: float = 30.0
    card_fill_norm_s: Optional[float] = None  # задаёт преподаватель: порог на стенде (63–83 с) не уточнён


ALLOWED_ROLES = {
    Mode.CARDS: {OPERATOR_112},
    Mode.CARD_ACTIONS: {DDS},
    Mode.COMBINED: {OPERATOR_112, DDS},
}


class Lesson:
    def __init__(self, lesson_id: str, owner_id: str, clock: Callable[[], float] = time.monotonic):
        self.id = lesson_id
        self.owner_id = owner_id
        self._clock = clock
        self.state = State.DRAFT
        self.config = Config()
        self.seats: Dict[str, Seat] = {}
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

    # --- проверки ---------------------------------------------------------
    def _require_owner(self, actor: Actor) -> None:
        # Администратор не управляет ходом занятия, преподаватель — только своими (ТЗ, ограничения ролей).
        if actor.kind is not ActorKind.TEACHER or actor.user_id != self.owner_id:
            raise PermissionDenied("управлять занятием может только его преподаватель")

    def _require_state(self, *states: State) -> None:
        if self.state not in states:
            raise InvalidTransition("действие недоступно в состоянии «%s»" % self.state.value)

    def _seat(self, participant_id: str) -> Seat:
        if participant_id not in self.seats:
            raise LessonError("участник %s не в занятии" % participant_id)
        return self.seats[participant_id]

    # --- жизненный цикл ---------------------------------------------------
    def configure(self, actor: Actor, **changes) -> None:
        self._require_owner(actor)
        self._require_state(State.DRAFT)
        self.config = dataclasses.replace(self.config, **changes)

    def open(self, actor: Actor) -> None:
        self._require_owner(actor)
        self._require_state(State.DRAFT)
        self.state = State.RECRUITING

    def join(self, participant_id: str) -> Seat:
        """Вход участника. Во время занятия — только возврат на своё место."""
        if participant_id in self.seats and self.state in (State.RECRUITING, State.RUNNING):
            self.reconnect(participant_id)
            return self.seats[participant_id]
        self._require_state(State.RECRUITING)
        seat = Seat(participant_id)
        self.seats[participant_id] = seat
        return seat

    def assign_role(self, actor: Actor, participant_id: str, role: Role) -> None:
        self._require_owner(actor)
        self._require_state(State.RECRUITING)
        if role.kind not in ALLOWED_ROLES[self.config.mode]:
            raise InvalidRole("роль «%s» не предусмотрена режимом «%s»" % (role.kind, self.config.mode.value))
        if role.kind == DDS and not role.service:
            raise InvalidRole("для роли ДДС нужна служба")
        self._seat(participant_id).role = role

    def start(self, actor: Actor) -> None:
        self._require_owner(actor)
        self._require_state(State.RECRUITING)
        if not self.seats:
            raise InvalidTransition("в занятии нет участников")
        unassigned = sorted(p for p, s in self.seats.items() if s.role is None)
        if unassigned:
            raise InvalidTransition("участникам не назначены роли: %s" % ", ".join(unassigned))
        self.state = State.RUNNING
        self.started_at = self._clock()

    def finish(self, actor: Actor) -> None:
        self._require_owner(actor)
        self._require_state(State.RUNNING)
        self.state = State.FINISHED
        self.finished_at = self._clock()

    def mark_reported(self) -> None:
        self._require_state(State.FINISHED)
        self.state = State.REPORTED

    # --- связь (§18.6) ------------------------------------------------------
    def disconnect(self, participant_id: str) -> None:
        seat = self._seat(participant_id)
        if seat.connected:
            seat.connected = False
            seat.disconnected_at = self._clock()

    def connection(self, participant_id: str) -> Connection:
        seat = self._seat(participant_id)
        if seat.connected:
            return Connection.ONLINE
        if self._clock() - seat.disconnected_at <= RECONNECT_GRACE_S:
            return Connection.RECONNECTING
        return Connection.OFFLINE

    def reconnect(self, participant_id: str) -> bool:
        """Возврат на место. True — уложился в окно без потери данных. Место держится всё занятие."""
        seat = self._seat(participant_id)
        within = seat.connected or self._clock() - seat.disconnected_at <= RECONNECT_GRACE_S
        seat.connected = True
        seat.disconnected_at = None
        return within

    # --- роли и видимость (§18.3, §18.4) -----------------------------------
    def bot_roles(self) -> FrozenSet[str]:
        """Роли, которые закрывают боты: пустой слот человека закрывает бот."""
        human = {s.role.kind for s in self.seats.values() if s.role}
        bots = {BOT_CALLER}  # звонит оператору 112, отвечает на звонок ДДС
        if DDS in human:
            bots.add(BOT_BRIGADE)
        if OPERATOR_112 not in human and self.config.mode is not Mode.CARDS:
            bots.add(OPERATOR_112)
        return frozenset(bots)

    def recipients(self, routing: Iterable[str]) -> FrozenSet[str]:
        """Кому из участников попадает карточка: служба роли ДДС входит в маршрутизацию карточки."""
        services = set(routing)
        return frozenset(p for p, s in self.seats.items()
                         if s.role and s.role.kind == DDS and s.role.service in services)
