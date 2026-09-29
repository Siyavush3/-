"""Оценка по категориям памятки ДДС. Кейсы — те самые ошибки диспетчеров, что разобраны в памятке."""
import pytest

from domain.evaluation import Report, Rubric, Step, evaluate_card, missing_facts

NUMBERS = {"A": "36900101"}


@pytest.fixture(scope="module")
def rubric():
    return Rubric.load()


def cats(findings):
    return [f.category for f in findings]


def test_clean_accepted_card(rubric):
    reports = [Report(60, "Начало реагирования", "выехали"), Report(120, "Работы завершены", "готово")]
    steps = [Step("Принята", 12, "направлен сантехник"), Step("Начало реагирования", 70, "бригада выехала"),
             Step("Работы завершены", 150, "течь устранена")]
    assert evaluate_card({"primary": "Принята", "final": "Работы завершены"}, 0, steps, reports, NUMBERS, rubric) == []


def test_no_status_and_late_status(rubric):
    assert cats(evaluate_card({"primary": "Принята"}, 0, [], [], NUMBERS, rubric)) == [1]
    late = evaluate_card({"primary": "Принята"}, 0, [Step("Принята", 557)], [], NUMBERS, rubric)  # трек со стенда
    assert cats(late) == [1] and "557 с" in late[0].detail


def test_accepted_what_should_be_refused(rubric):
    # памятка №2: «Принята: не обслуживаем территорию» — следовало «Не принята»
    steps = [Step("Принята", 10, "не обслуживаем территорию")]
    assert cats(evaluate_card({"primary": "Не принята", "comment_facts": [["не обслуж"]]}, 0, steps, [], NUMBERS, rubric)) == [2]


def test_refused_profile_incident(rubric):
    # памятка №3: посторонние в подвале — «Не принята» без комментариев
    assert cats(evaluate_card({"primary": "Принята"}, 0, [Step("Не принята", 10)], [], NUMBERS, rubric)) == [3, 4]


def test_refusal_comment_missing_and_incomplete(rubric):
    expect = {"primary": "Не принята", "comment_facts": [["пик"], ["передан", "сообщ"]]}
    assert cats(evaluate_card(expect, 0, [Step("Не принята", 10, "")], [], NUMBERS, rubric)) == [4]
    incomplete = evaluate_card(expect, 0, [Step("Не принята", 10, "не обслуживаем")], [], NUMBERS, rubric)
    assert cats(incomplete) == [5] and "пик" in incomplete[0].detail
    full = [Step("Не принята", 10, "Дом обслуживает УК «ПИК», информация передана в их диспетчерскую")]
    assert evaluate_card(expect, 0, full, [], NUMBERS, rubric) == []


def test_duplicate_by_card_number(rubric):
    expect = {"primary": "Не принята", "comment_facts": [["дубл", "{A}"]]}
    assert evaluate_card(expect, 0, [Step("Не принята", 9, "реагирование по КП 36900101")], [], NUMBERS, rubric) == []


def test_refusal_after_brigade_twist(rubric):
    # памятка №4: провод «Ростелекома» — «Отказ от выполнения работ» с комментарием
    expect = {"primary": "Принята", "final": "Отказ от выполнения работ",
              "final_comment_facts": [["ростелеком"], ["передан", "сообщ"]]}
    reports = [Report(50, "Начало реагирования", "выехал электрик"),
               Report(110, "Отказ от выполнения работ", "провод «Ростелекома», сообщили им")]
    steps = [Step("Принята", 5, "направлен электрик"), Step("Начало реагирования", 60, "выехал"),
             Step("Работы завершены", 130, "всё")]
    found = evaluate_card(expect, 0, steps, reports, NUMBERS, rubric)
    assert sorted(cats(found)) == [2, 6]  # не тот итог и нет «Отказа» после доклада


def test_progress_statuses_follow_reports(rubric):
    # памятка №6: ход работ не отражён
    reports = [Report(60, "Начало реагирования", "выехали"), Report(120, "Прибытие", "на месте")]
    steps = [Step("Принята", 5, "направлена бригада"), Step("Прибытие", 250, "")]
    found = evaluate_card({"primary": "Принята"}, 0, steps, reports, NUMBERS, rubric)
    assert cats(found) == [6, 6, 6]  # нет «Начала», «Прибытие» поздно, «Прибытие» без комментария


def test_status_ahead_of_brigade(rubric):
    reports = [Report(100, "Работы завершены", "закончили")]
    steps = [Step("Принята", 5, "ок"), Step("Работы завершены", 40, "закончили")]
    assert cats(evaluate_card({"primary": "Принята"}, 0, steps, reports, NUMBERS, rubric)) == [2]


def test_score_and_facts_helper(rubric):
    assert missing_facts("Передано в «Практику»", [["практик"], ["передан"]], {}) == []
    assert rubric.score([]) == 100
