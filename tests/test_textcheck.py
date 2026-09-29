"""Проверка текста (узел 3): орфография, оформление, факты с учётом словоформ."""
import json

import pytest

pytest.importorskip("pymorphy3")

from arm112.paths import data_dir  # noqa: E402
from domain.calls import CallPack, CardTiming, evaluate_card as evaluate_112, load_rubric112  # noqa: E402
from domain.evaluation import Rubric, Step, evaluate_card, missing_facts  # noqa: E402
from domain.textcheck import TextChecker, domain_words  # noqa: E402
from tests_support import perfect_card  # noqa: E402


@pytest.fixture(scope="module")
def checker(classifier):
    pack = CallPack.load(data_dir() / "scenarios" / "calls_bilety.json")
    with open(data_dir() / "services" / "services.json", encoding="utf-8") as f:
        services = [x["short"] for x in json.load(f)["services"]]
    return TextChecker(domain_words(classifier, [pack], services))


def words(issues):
    return [i["word"] for i in issues]


def test_typo_from_live_stand_is_caught(checker):
    """Опечатка с боевого стенда (UI-SPEC §13.3): «Отправлн сантехник для перекрытия воды»."""
    issues = checker.spelling("Отправлн сантехник для перекрытия воды")
    assert words(issues) == ["Отправлн"] and "отправлен" in issues[0]["suggestions"]


def test_real_word_typo_known_only_as_name(checker):
    """«гарит» есть в словаре только как форма имени «Гарита» — строчными это опечатка."""
    issues = checker.spelling("в квартире гарит проводка")
    assert words(issues) == ["гарит"] and "горит" in issues[0]["suggestions"]


def test_no_false_alarms_on_dispatcher_text(checker):
    texts = [
        "Дом ул. Рогова 17 обслуживает УК «ПИК», информация передана в их диспетчерскую.",
        "Направлена аварийная бригада ГБУ «Жилищник», перекрыли стояк горячей воды.",
        "Задымление мусоропровода, открытого пламени нет, пострадавших нет, МКАД 49 км.",
        "Электрощитовая обесточена, ОЭК уведомлены, работы ведутся.",
        "Горит а/м Фольксваген, водитель получил ожог рук.",
    ]
    for t in texts:
        assert checker.spelling(t) == [], t


def test_grammar_rules(checker):
    kinds = [i["rule"] for i in checker.grammar("Бригада  выехала ,сантехник на месте на месте. Пoжар")]
    assert kinds == ["double_space", "space_before_punct", "no_space_after_comma", "repeated_word", "mixed_alphabet"]
    assert checker.grammar("Бригада выехала, сантехник на месте.") == []


def test_facts_match_word_forms(checker):
    groups = [["пик"], ["передан", "сообщ"]]
    assert checker.missing("УК ПИК, информацию передали диспетчеру", groups) == []   # «передали» → «передать»
    assert checker.missing("УК ПИК", groups) == [["передан", "сообщ"]]
    assert missing_facts("передали в УК ПИК", groups, {}, checker) == []
    assert missing_facts("передали в УК ПИК", groups, {}) == ["передан"]                # без морфологии — не засчитано


def test_dds_comment_errors_are_category_8(checker):
    rubric = Rubric.load()
    expect = {"primary": "Не принята", "comment_facts": [["пик"], ["передан", "сообщ"]]}
    steps = [Step("Не принята", 10.0, "Дом обслуживает УК ПИК, информацыю передали  диспетчеру")]
    findings = evaluate_card(expect, 0.0, steps, [], {}, rubric, card="1", checker=checker)
    assert sorted(f.category for f in findings) == [8, 8]                           # опечатка и два пробела
    assert any("информацию" in f.detail for f in findings)
    many = [f for f in findings] * 5
    assert rubric.score(many) == 100 - rubric.text_cap * 2                          # не больше пяти ошибок в зачёт


def test_112_description_checked(checker, classifier):
    with open(data_dir() / "classifier" / "ui_services.json", encoding="utf-8") as f:
        from arm112.reference.territory import Territory
        from domain.scenario import Router
        router = Router(classifier, Territory.load(), json.load(f))
    pack = CallPack.load(data_dir() / "scenarios" / "calls_bilety.json")
    call = pack.by_key["b01-1"]
    card = dict(perfect_card(call), description="Возгарание мусорного контейнера, горит, пострадавших нет.")
    auto = [p.name for p in router.plaques(card["code"], card["flags"], None) if p.row == 1]
    findings = evaluate_112(call, card, CardTiming(0, 5, 40), classifier, load_rubric112(), auto, 80, 15,
                            checker=checker)
    assert [f.category for f in findings] == [8] and "Возгарание" in findings[0].detail


def test_text_check_api(classifier, tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_api import login, make_db, make_engine
    engine, clock, pack = make_engine(classifier)
    engine.textcheck = TextChecker()
    db, ids = make_db(tmp_path / "text.sqlite")
    with TestClient(create_app(engine, db, tick_s=0.01)) as client:
        th, _, _ = login(client, "t1")
        res = client.post("/api/text/check", json={"text": "Бригада вызвана , сантехнек на месте"}, headers=th).json()
        assert [s["word"] for s in res["spelling"]] == ["сантехнек"] and res["grammar"][0]["rule"] == "space_before_punct"


def test_sentence_start(checker):
    """Заглавная в начале предложения — от позиции: опечатку ловим, имена и города — нет."""
    found = words(checker.spelling("Гарит квартира. Москва, Ленинский проспект. Липатина Ольга чистила ухо."))
    assert found == ["Гарит"]


def test_ticket_texts_have_no_false_alarms(checker):
    pack = CallPack.load(data_dir() / "scenarios" / "calls_bilety.json")
    flagged = [(c["key"], e["word"]) for c in pack.calls for e in checker.spelling(c["situation"])]
    assert flagged == []
