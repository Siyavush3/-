"""Маршрут карточки со всеми слоями и пакет сценариев."""
import copy
import json

import pytest

from arm112.paths import data_dir
from arm112.reference.fsm import ResponseFsm
from arm112.reference.territory import Place, Territory
from domain.scenario import Pack, Router, ScenarioError


@pytest.fixture(scope="module")
def territory():
    return Territory.load()


@pytest.fixture(scope="module")
def router(classifier, territory):
    with open(data_dir() / "classifier" / "ui_services.json", encoding="utf-8") as f:
        return Router(classifier, territory, json.load(f))


def test_territorial_rule_of_the_customer(territory):
    # Ответ заказчика: пожар в Щукино уходит в ДДС управы Щукино и ДДС Северо-Западного округа.
    assert territory.resolve(["ОИВ района и округа"], Place("Щукино", "СЗАО")) == ("Поселение СЗАО", "Поселение Щукино")
    # Для ТиНАО работает свой класс, «городской» к нему не применяется.
    assert territory.resolve(["ОИВ района и округа"], Place("Вороновское", "ТиНАО")) == ()
    assert territory.resolve(["Автодороги округа"], Place("Щукино", "СЗАО")) == ("ГБУ АД СЗАО",)


def test_plaques_match_stand_card(router):
    """Карточка со стенда sdds_12: «пожар: квартира», поселение Вороновское, угроза людям, газификация,
    «Деп. ЖКХ» добавлена вручную. Совпадают состав, порядок, главная и ручная службы."""
    plaques = router.plaques("1050101", ["УЛ", "ГАЗ"], Place("Вороновское", "ТиНАО"), manual=["Деп. ЖКХ"])
    assert [p.name for p in plaques] == [
        "Служба 101", "Служба 104", "Служба 102", "Деп. ЖКХ", "ЦЭМП", "ЦОДД", "Мос.Без.", "Мослифт", "ОАТИ",
        "Поселение ТиНАО", "Поселение Вороновское"]
    assert [p.name for p in plaques if p.main] == ["Служба 101"]
    assert [p.name for p in plaques if p.manual] == ["Деп. ЖКХ"]


def test_object_dependent_services_need_the_object(router):
    names = {p.name for p in router.plaques("1050101", [], Place("Щукино", "СЗАО"))}
    assert "ЭВАЖД" not in names and "Мособлгаз" not in names
    names = {p.name for p in router.plaques("1050101", [], Place("Щукино", "СЗАО", ("высотка ЭВАЖД",)))}
    assert "ЭВАЖД" in names


def load_pack():
    return Pack.load(data_dir() / "scenarios" / "dds_shchukino_v0.json")


def test_shchukino_pack_is_valid(classifier, territory, router):
    pack = load_pack()
    pack.validate(classifier, territory, ResponseFsm.load(), router)
    assert len(pack.cards) == 10 and pack.role == "Поселение Щукино"


@pytest.mark.parametrize("breakage, message", [
    (lambda c: c.update(code="9999999"), "нет кода"),
    (lambda c: c.update(flags=["НЕТ_ТАКОГО"]), "неизвестные флаги"),
    (lambda c: c.update(code="15140000", manual_services=[]), "не доходит до роли"),
    (lambda c: c["brigade"][1].update(status="Проведение работ"), "недопустимый"),  # после «Начало» — только «Прибытие»
])
def test_broken_pack_is_rejected(classifier, territory, router, breakage, message):
    pack = load_pack()
    raw = copy.deepcopy(pack.raw)
    card = next(c for c in raw["cards"] if c["key"] == "A")
    breakage(card)
    with pytest.raises(ScenarioError, match=message):
        Pack(raw).validate(classifier, territory, ResponseFsm.load(), router)
