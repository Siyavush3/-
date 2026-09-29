"""Занятие как локальное лобби (ARCHITECTURE.md §18)."""
import pytest

from domain.lesson import (BOT_BRIGADE, BOT_CALLER, OPERATOR_112, Actor, ActorKind, Connection,
                           InvalidRole, InvalidTransition, Lesson, Mode, PermissionDenied, Role, State)

TEACHER = Actor("t1", ActorKind.TEACHER)
OTHER_TEACHER = Actor("t2", ActorKind.TEACHER)
ADMIN = Actor("a1", ActorKind.ADMIN)
STUDENT = Actor("s1", ActorKind.STUDENT)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def running_lesson(mode=Mode.CARD_ACTIONS, roles=None, clock=None):
    lesson = Lesson("L1", TEACHER.user_id, clock or Clock())
    lesson.configure(TEACHER, mode=mode)
    lesson.open(TEACHER)
    roles = roles or {"s1": Role.dds("ЦОДД"), "s2": Role.dds("ОАТИ"), "s3": Role.dds("Мосгортранс")}
    for pid, role in roles.items():
        lesson.join(pid)
        lesson.assign_role(TEACHER, pid, role)
    lesson.start(TEACHER)
    return lesson


def test_lifecycle():
    lesson = running_lesson()
    assert lesson.state is State.RUNNING and lesson.started_at == 1000.0
    lesson.finish(TEACHER)
    assert lesson.state is State.FINISHED
    lesson.mark_reported()
    assert lesson.state is State.REPORTED


@pytest.mark.parametrize("actor", [OTHER_TEACHER, ADMIN, STUDENT])
def test_only_owner_controls_lesson(actor):
    # ТЗ: администратор не меняет ход занятия, преподаватель не вмешивается в чужие.
    lesson = running_lesson()
    with pytest.raises(PermissionDenied):
        lesson.finish(actor)


def test_transitions_only_forward():
    lesson = Lesson("L1", TEACHER.user_id)
    with pytest.raises(InvalidTransition):
        lesson.start(TEACHER)          # сначала набор
    lesson.open(TEACHER)
    with pytest.raises(InvalidTransition):
        lesson.configure(TEACHER, mode=Mode.CARDS)  # настройки — только в черновике
    with pytest.raises(InvalidTransition):
        lesson.start(TEACHER)          # нет участников


def test_start_requires_roles():
    lesson = Lesson("L1", TEACHER.user_id)
    lesson.open(TEACHER)
    lesson.join("s1")
    with pytest.raises(InvalidTransition):
        lesson.start(TEACHER)


def test_roles_follow_mode():
    lesson = Lesson("L1", TEACHER.user_id)
    lesson.configure(TEACHER, mode=Mode.CARD_ACTIONS)
    lesson.open(TEACHER)
    lesson.join("s1")
    with pytest.raises(InvalidRole):
        lesson.assign_role(TEACHER, "s1", Role.operator_112())
    with pytest.raises(InvalidRole):
        lesson.assign_role(TEACHER, "s1", Role("ДДС", None))


def test_no_new_participants_once_running():
    lesson = running_lesson()
    with pytest.raises(InvalidTransition):
        lesson.join("newcomer")


def test_bots_fill_empty_slots():
    assert running_lesson().bot_roles() == {BOT_CALLER, BOT_BRIGADE, OPERATOR_112}
    cards = running_lesson(Mode.CARDS, {"s1": Role.operator_112()})
    assert cards.bot_roles() == {BOT_CALLER}
    combined = running_lesson(Mode.COMBINED, {"s1": Role.operator_112(), "s2": Role.dds("ЦОДД")})
    assert combined.bot_roles() == {BOT_CALLER, BOT_BRIGADE}


def test_feed_visibility_uses_routing(classifier):
    """Ролевая модель ТЗ: карточка видна тем, чья служба в её маршрутизации (кадр стенда 19)."""
    lesson = running_lesson()
    routing = classifier.route("1010101", ["НД"]).services  # {Служба 101, ЦОДД, ОАТИ}
    assert lesson.recipients(routing) == {"s1", "s2"}      # ЦОДД и ОАТИ, но не Мосгортранс


def test_reconnect_window():
    clock = Clock()
    lesson = running_lesson(clock=clock)
    lesson.disconnect("s1")
    clock.now += 20
    assert lesson.connection("s1") is Connection.RECONNECTING
    assert lesson.join("s1").connected and lesson.connection("s1") is Connection.ONLINE

    lesson.disconnect("s1")
    clock.now += 45
    assert lesson.connection("s1") is Connection.OFFLINE
    assert lesson.reconnect("s1") is False           # вернулся, но позже окна — это фиксируется
    assert lesson.seats["s1"].role == Role.dds("ЦОДД")  # место и роль держатся всё занятие
