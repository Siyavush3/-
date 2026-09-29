"""Генерация сценариев с эталоном (узел 4): корректность пакетов, эталон, правки по комментарию,
утверждение преподавателем, поток карточек ДДС из карточек обучающихся."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from arm112.paths import data_dir
from arm112.reference.fsm import ResponseFsm
from arm112.reference.territory import Territory
from domain import generator as gen
from domain.calls import CallPack, CardTiming, evaluate_card as evaluate_112, load_rubric112
from domain.engine import Engine
from domain.evaluation import Rubric, missing_facts
from domain.llm import LocalLLM
from domain.scenario import CardViews, Pack, Router
from tests_support import perfect_card, perfect_run


@pytest.fixture(scope="module")
def router(classifier):
    with open(data_dir() / "classifier" / "ui_services.json", encoding="utf-8") as f:
        return Router(classifier, Territory.load(), json.load(f))


@pytest.fixture(scope="module")
def gz():
    return gen.Gazetteer.load()


@pytest.fixture(scope="module")
def checker(classifier, gz):
    pytest.importorskip("pymorphy3")
    from domain.textcheck import TextChecker, domain_words
    with open(data_dir() / "services" / "services.json", encoding="utf-8") as f:
        services = [x["short"] for x in json.load(f)["services"]]
    return TextChecker(domain_words(classifier, [], services) | set(gen.vocabulary(gz)))


def validate(raw, classifier, router):
    pack = Pack(raw)
    pack.validate(classifier, router.territory, ResponseFsm.load(), router)
    return pack


def kinds(raw):
    return [c["gen"]["kind"] for c in raw["cards"]]


# ---------- ДДС ----------
def test_dds_packs_are_valid_everywhere(classifier, router, gz):
    """Каждый район × каждая сложность × разные категории: пакет проходит ту же проверку, что пакеты из файлов."""
    for district in gz.districts:
        for level in (1, 2, 3):
            for groups in (None, ["14"], ["1"]):
                raw = gen.generate_dds(router, gz, district, level, 10, groups, seed=level)
                pack = validate(raw, classifier, router)
                assert len(pack.cards) == 10 and kinds(raw)[0] == "profile"
                assert pack.role == router.territory.district_service(district)
                if groups == ["1"]:
                    assert {classifier.incidents[c["code"]]["group"] for c in raw["cards"]} == {"1"}


def test_dds_generation_is_deterministic(router, gz):
    a = gen.generate_dds(router, gz, "Строгино", 2, 8, None, seed=5)
    assert a == gen.generate_dds(router, gz, "Строгино", 2, 8, None, seed=5)
    assert a != gen.generate_dds(router, gz, "Строгино", 2, 8, None, seed=6)


def test_difficulty_changes_the_mix(router, gz):
    easy = [k for s in range(10) for k in kinds(gen.generate_dds(router, gz, "Щукино", 1, 10, None, s))]
    hard = [k for s in range(10) for k in kinds(gen.generate_dds(router, gz, "Щукино", 3, 10, None, s))]
    assert easy.count("profile") > hard.count("profile")
    assert set(easy) <= {"profile", "duplicate", "other_uk"}                         # база: без редких ловушек
    assert {"lift_org", "road", "oek", "refusal"} <= set(hard)
    for s in range(10):                                                              # в «высокой» всегда есть отказ
        assert "refusal" in kinds(gen.generate_dds(router, gz, "Марьино", 3, 6, None, s))


def test_trap_answers_follow_the_service_reference(router, gz):
    """Эталон ловушки выводится из справки службы: обучающийся может найти ответ на экране."""
    for seed in range(8):
        raw = gen.generate_dds(router, gz, "Отрадное", 3, 12, None, seed)
        ref = " ".join(i for s in raw["reference"]["sections"] for i in s["items"]).lower().replace("ё", "е")
        for card in raw["cards"]:
            kind, facts = card["gen"]["kind"], card["expect"].get("comment_facts", [])
            if kind in ("other_uk", "lift_org"):
                assert facts[0][0] in ref                                           # название организации
                house = "%s, д. %s" % (gen.short_street(card["address"]["street"]), card["address"]["house"])
                assert house.lower() in ref                                          # и этот дом в справке
            if kind == "road":
                assert "автомобильные дороги" in ref and card["expect"]["primary"] == "Не принята"
            if kind == "duplicate":
                src = next(c for c in raw["cards"] if c["key"] == card["gen"]["of"])
                assert src["at"] < card["at"] and src["address"] == card["address"]


def test_sample_comments_satisfy_the_answer_key(router, gz, checker):
    """Образец комментария из эталона засчитывается проверкой и сам без ошибок."""
    numbers = {gen._key(i): str(36901101 + i) for i in range(30)}
    for seed in range(6):
        raw = gen.generate_dds(router, gz, "Тверской", 3, 12, None, seed)
        for card in raw["cards"]:
            exp = card["expect"]
            for text_key, facts_key in (("sample_comment", "comment_facts"), ("sample_final_comment", "final_comment_facts")):
                if text_key in exp:
                    text = re.sub(r"\{(\w+)\}", lambda m: numbers[m.group(1)], exp[text_key])  # {A} → номер карточки
                    assert missing_facts(text, exp[facts_key], numbers) == [], (card["key"], text)
                    assert checker.spelling(text) == [], text


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_perfect_student_scores_100_on_generated_pack(classifier, router, gz, checker):
    for seed in (1, 2):
        raw = gen.generate_dds(router, gz, "Щукино", 3, 10, None, seed)
        pack = validate(raw, classifier, router)
        clock = Clock()
        engine = Engine(router, CardViews.load(classifier), ResponseFsm.load(), Rubric.load(), {pack.id: pack},
                        clock=clock, wall=lambda: 1_758_000_000.0, textcheck=checker)
        result = perfect_run(engine, clock, pack)
        assert result["score"] == 100, result["findings"]


def test_generated_texts_have_no_spelling_errors(classifier, router, gz, checker):
    for district in gz.districts:
        raw = gen.generate_dds(router, gz, district, 3, 12, None, seed=3)
        assert [i for p in gen.preview(raw, classifier, None, checker) for i in p["issues"]] == []
        texts = [r["text"] for c in raw["cards"] for r in c.get("brigade", [])]
        assert [e for t in texts for e in checker.spelling(t)] == []
    for level in (1, 2, 3):
        raw = gen.generate_calls(gz, None, level, 20, seed=level)
        assert [i for p in gen.preview(raw, classifier, None, checker) for i in p["issues"]] == []


# ---------- оператор 112 ----------
def test_calls_pack_valid_and_perfect_card_scores_100(classifier, router, gz):
    rubric = load_rubric112()
    for level in (1, 2, 3):
        raw = gen.generate_calls(gz, None, level, 16, seed=level)
        pack = CallPack(raw)
        pack.validate(classifier)
        for call in pack.calls:
            card = perfect_card(call)
            auto = [p.name for p in router.plaques(card["code"], card["flags"], None) if p.row == 1]
            findings = evaluate_112(call, card, CardTiming(0, 5, 40), classifier, rubric, auto, 80, 15)
            assert findings == [], (call["key"], findings)


def test_call_groups_and_difficulty(classifier, gz):
    raw = gen.generate_calls(gz, ["22", "17"], 1, 6, seed=1)
    assert {classifier.incidents[c["gold"]["codes"][0]]["group"] for c in raw["calls"]} == {"22", "17"}
    easy, hard = (gen.generate_calls(gz, ["14"], lvl, 1, seed=4)["calls"][0]["dialogue"] for lvl in (1, 3))
    assert easy["address"] == easy["address_clarified"]                      # база: адрес сразу точный
    assert hard["address"] != hard["address_clarified"]                      # сложнее: сначала ориентир
    assert len(hard["opening"]) < len(hard["details"]) + 12                  # высокая: суть надо выспрашивать
    with pytest.raises(gen.GeneratorError):
        gen.generate_calls(gz, ["99"], 1, 3)


# ---------- правки по комментарию ----------
def test_parse_comment():
    assert gen.parse_comment("Сделай сложнее, пусть будет ловушка") == ["harder"]
    assert gen.parse_comment("проще и другой адрес") == ["easier", "address"]
    assert gen.parse_comment("без пострадавших") == ["no_victims"]
    assert gen.parse_comment("пусть заявитель говорит вежливее") == []


def test_revise_dds_by_comment(classifier, router, gz):
    raw = gen.generate_dds(router, gz, "Щукино", 1, 8, None, seed=11)
    free = next(c["key"] for c in raw["cards"] if c["gen"]["kind"] == "profile" and not gen._referenced(raw, c["key"]))
    harder, changes = gen.revise(raw, free, "Сделай сложнее", router, gz)
    card = harder["cards"][gen._find(harder["cards"], free)]
    assert card["gen"]["kind"] != "profile" and "сложнее" in changes[0] and card["history"][0]["comment"] == "Сделай сложнее"
    validate(harder, classifier, router)
    easier, changes = gen.revise(harder, free, "сделай проще", router, gz)
    assert easier["cards"][gen._find(easier["cards"], free)]["gen"]["kind"] == "profile"
    moved, changes = gen.revise(raw, free, "другой адрес", router, gz)
    new_addr = moved["cards"][gen._find(moved["cards"], free)]["address"]
    assert [new_addr["street"], new_addr["house"]] in raw["generated"]["own"] and "адрес" in changes[0]
    same, changes = gen.revise(raw, free, "пусть заявитель говорит вежливее", router, gz)
    assert "не распознан" in changes[0] and same["cards"][gen._find(same["cards"], free)]["history"]
    with pytest.raises(gen.GeneratorError):
        gen.revise(raw, free, "   ", router, gz)


class FakeLLM:
    def __init__(self, text):
        self.text, self.calls = text, []

    def available(self):
        return True

    def rewrite(self, text, comment):
        self.calls.append((text, comment))
        return self.text


def test_llm_rewrites_only_what_rules_did_not_understand(router, gz, checker):
    raw = gen.generate_dds(router, gz, "Щукино", 1, 5, None, seed=2)
    llm = FakeLLM("Сильно течёт вода с потолка на кухне, заявитель волнуется")
    new, changes = gen.revise(raw, "A", "пусть заявитель говорит вежливее", router, gz, llm, checker)
    assert "языковой моделью" in changes[0] and new["cards"][0]["description"] == llm.text
    _, changes = gen.revise(raw, "A", "сделай сложнее", router, gz, llm, checker)
    assert len(llm.calls) == 1                                                       # правила поняли — модель не зовём
    bad, changes = gen.revise(raw, "A", "вежливее", router, gz, FakeLLM("Сильно течот вада"), checker)
    assert "с ошибками" in changes[0] and bad["cards"][0]["description"] == raw["cards"][0]["description"]


def test_revise_call_by_comment(classifier, gz):
    raw = gen.generate_calls(gz, ["14", "22"], 1, 4, seed=3)
    key = next(c["key"] for c in raw["calls"] if c["gen"]["group"] == "14")
    new, changes = gen.revise(raw, key, "Сделай сложнее и другой адрес", None, gz)
    call = new["calls"][gen._find(new["calls"], key)]
    assert call["gen"]["difficulty"] == 2 and call["dialogue"]["address"] != call["dialogue"]["address_clarified"]
    assert call["gold"]["address"]["street"] in call["dialogue"]["address_clarified"] and len(changes) == 2
    CallPack(new).validate(classifier)
    med = next(c["key"] for c in raw["calls"] if c["gen"]["group"] == "22")
    _, changes = gen.revise(raw, med, "без пострадавших", None, gz)
    assert "заданы типом" in changes[0]                                              # у «плохо с сердцем» пострадавший всегда
    typed, changes = gen.revise(raw, med, "другой тип происшествия", None, gz)
    assert typed["calls"][gen._find(typed["calls"], med)]["gold"]["codes"] != raw["calls"][gen._find(raw["calls"], med)]["gold"]["codes"]


def test_exclude_and_partial_approval_keep_duplicates_consistent(classifier, router, gz):
    raw = next(r for r in (gen.generate_dds(router, gz, "Щукино", 1, 10, None, s) for s in range(30))
               if "duplicate" in kinds(r))
    dup = next(c for c in raw["cards"] if c["gen"]["kind"] == "duplicate")
    with pytest.raises(gen.GeneratorError):
        gen.exclude(raw, dup["gen"]["of"])                                          # на исходную ссылается дубль
    without = gen.exclude(raw, dup["key"])
    assert len(without["cards"]) == 9 and [c["at"] for c in without["cards"]] == [10 + 60 * i for i in range(9)]
    validate(without, classifier, router)
    with pytest.raises(gen.GeneratorError):
        gen.keep_only(raw, [dup["key"]])                                            # дубль без исходной — нельзя
    kept = gen.keep_only(raw, [dup["gen"]["of"], dup["key"]])
    assert [c["key"] for c in kept["cards"]] == [dup["gen"]["of"], dup["key"]]
    validate(kept, classifier, router)


# ---------- карточки обучающихся → поток ДДС ----------
def student_row(code, lesson_id=7, categories=()):
    card = {"number": "36907101", "code": code, "flags": [], "victims": False,
            "caller": {"name": "Иванов Иван", "status": "очевидец"},
            "address": {"subject": "Москва", "street": "улица Рогова", "house": "5", "entrance": "2"},
            "description": "Течёт вода с потолка, квартира 5."}
    return {"lesson_id": lesson_id, "user_id": 4, "code": code,
            "card": {"participant": "u4", "number": "36907101", "code": code, "score": 90,
                     "categories": list(categories), "card": card}}


def test_student_cards_feed_the_dds_flow(classifier, router):
    base = Pack.load(data_dir() / "scenarios" / "dds_shchukino_v0.json").raw
    rows = [student_row("14020300"), student_row("22370000"),                   # «плохо с сердцем» до ДДС не доходит
            student_row("14100100", categories=[1]),                            # тип выбран неверно — не берём
            student_row("14030201")]
    usable = gen.usable_student_rows(rows, base, router, classifier)
    assert [r["code"] for r in usable] == ["14020300", "14030201"]
    only = gen.with_student_cards(base, rows, "student", 5, router, classifier, seed="x")
    assert [c["key"] for c in only["cards"]] == ["S1", "S2"] and only["cards"][0]["expect"]["primary"] == "Принята"
    validate(only, classifier, router)
    mixed = gen.with_student_cards(base, rows, "mixed", 1, router, classifier, seed="x")
    assert [c["key"] for c in mixed["cards"]][:3] == [base["cards"][0]["key"], "S1", base["cards"][1]["key"]]
    assert len(mixed["cards"]) == len(base["cards"]) + 1
    validate(mixed, classifier, router)
    with pytest.raises(gen.GeneratorError):
        gen.with_student_cards(base, [student_row("22370000")], "student", 5, router, classifier, seed="x")


# ---------- локальная языковая модель ----------
def test_local_llm_speaks_openai_protocol():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["messages"][0]["role"] == "system" and "Замечание" in body["messages"][1]["content"]
            data = json.dumps({"choices": [{"message": {"content": "«Новый текст карточки»\nпояснение"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        llm = LocalLLM("http://127.0.0.1:%d" % server.server_port, timeout=5)
        assert llm.available() and llm.rewrite("старый текст", "короче") == "Новый текст карточки"
    finally:
        server.shutdown()
    assert LocalLLM("").available() is False
    assert LocalLLM("http://127.0.0.1:9", timeout=1).rewrite("текст", "замечание") is None   # модели нет — правила


# ---------- API: черновик → проверка → утверждение → занятие ----------
@pytest.fixture
def api(classifier, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_api import make_db, make_engine
    monkeypatch.delenv("A112_LLM_URL", raising=False)
    engine, clock, pack = make_engine(classifier)
    db, ids = make_db(tmp_path / "gen.sqlite")
    with TestClient(create_app(engine, db, tick_s=0.01)) as client:
        yield client, engine, clock, db


def test_scenario_lifecycle_over_api(api, classifier):
    from test_api import login
    client, engine, clock, db = api
    th, _, _ = login(client, "t1")
    t2h, _, _ = login(client, "t2")
    sh, _, _ = login(client, "s1")
    assert client.get("/api/scenarios", headers=sh).status_code == 403
    opts = client.get("/api/scenarios/options", headers=th).json()
    assert "Щукино" in [d["name"] for d in opts["districts"]] and opts["llm"] is False
    bad = client.post("/api/scenarios/generate", json={"mode": "card_actions", "district": "Нигде"}, headers=th)
    assert bad.status_code == 400

    sc = client.post("/api/scenarios/generate", json={"mode": "card_actions", "district": "Щукино", "difficulty": 2,
                                                      "count": 8, "seed": 3}, headers=th).json()
    assert sc["status"] == "draft" and sc["cards"] == 8 and sc["slug"] == "sc-%d" % sc["id"]
    assert sc["items"][0]["expect"]["why"] and sc["reference"]["sections"]
    packs = {p["id"] for p in client.get("/api/packs", headers=th).json()}
    assert sc["slug"] not in packs                                                   # черновик в занятия не попадает

    free = next(i["key"] for i in sc["items"] if i["kind"] == "profile"
                and not any("{%s}" % i["key"] in v for j in sc["items"] for g in j["expect"].get("comment_facts", []) for v in g))
    assert client.post("/api/scenarios/%d/cards/%s/revise" % (sc["id"], free), json={"comment": "сложнее"},
                       headers=t2h).status_code == 403                               # чужой черновик не правим
    rev = client.post("/api/scenarios/%d/cards/%s/revise" % (sc["id"], free), json={"comment": "Сделай сложнее"},
                      headers=th).json()
    assert "сложнее" in rev["changes"][0]
    assert next(i for i in rev["items"] if i["key"] == free)["kind"] != "profile"
    last = rev["items"][-1]["key"]
    if not any("{%s}" % last in v for j in rev["items"] for g in j["expect"].get("comment_facts", []) for v in g):
        ex = client.post("/api/scenarios/%d/cards/%s/exclude" % (sc["id"], last), headers=th).json()
        assert ex["cards"] == 7

    keep = [i["key"] for i in client.get("/api/scenarios/%d" % sc["id"], headers=th).json()["items"]
            if i["kind"] != "duplicate"][:4]
    ok = client.post("/api/scenarios/%d/approve" % sc["id"], json={"keys": keep, "title": "Щукино: тренировка"},
                     headers=th).json()
    assert ok["status"] == "approved" and ok["cards"] == 4 and ok["title"] == "Щукино: тренировка"
    assert client.post("/api/scenarios/%d/cards/%s/revise" % (sc["id"], keep[0]), json={"comment": "проще"},
                       headers=th).status_code == 409                                # утверждённый не правим
    packs = {p["id"]: p for p in client.get("/api/packs", headers=th).json()}
    assert packs[sc["slug"]]["cards"] == 4 and packs[sc["slug"]]["role"] == "Поселение Щукино"
    lesson = client.post("/api/lessons", json={"pack_id": sc["slug"], "time_scale": 0.1}, headers=th).json()
    assert lesson["pack"]["title"] == "Щукино: тренировка"

    copy_ = client.post("/api/scenarios/%d/copy" % sc["id"], headers=t2h).json()
    assert copy_["status"] == "draft" and copy_["author"] == "Козлов" and copy_["cards"] == 4
    arch = client.post("/api/scenarios/%d/archive" % sc["id"], headers=th).json()
    assert arch["status"] == "archived"
    assert sc["slug"] not in {p["id"] for p in client.get("/api/packs", headers=th).json()}
    actions = [r["action"] for r in db.query("SELECT action FROM audit WHERE action LIKE 'scenario.%' ORDER BY id")]
    assert actions[:2] == ["scenario.generate", "scenario.revise"] and "scenario.approve" in actions


def test_approved_scenarios_survive_restart(classifier, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from test_api import login, make_db, make_engine
    monkeypatch.delenv("A112_LLM_URL", raising=False)
    db, _ = make_db(tmp_path / "restart.sqlite")
    engine, _, _ = make_engine(classifier)
    with TestClient(create_app(engine, db, tick_s=0.01)) as client:
        th, _, _ = login(client, "t1")
        sc = client.post("/api/scenarios/generate", json={"mode": "cards", "groups": ["14", "22"], "count": 3, "seed": 1},
                         headers=th).json()
        client.post("/api/scenarios/%d/approve" % sc["id"], json={}, headers=th)
    engine2, _, _ = make_engine(classifier)
    with TestClient(create_app(engine2, db, tick_s=0.01)) as client:
        th, _, _ = login(client, "t1")
        packs = {p["id"]: p for p in client.get("/api/packs", headers=th).json()}
        assert packs[sc["slug"]]["mode"] == "cards" and packs[sc["slug"]]["cards"] == 3


def test_student_cards_become_dds_lesson(api, classifier):
    """Сценарий 2 ТЗ целиком: карточка, заполненная обучающимся-оператором 112, приходит обучающемуся ДДС."""
    from test_api import hello, login, recv
    client, engine, clock, db = api
    th, _, _ = login(client, "t1")
    sh, stok, sid = login(client, "s1")
    dds = "dds-shchukino-v0"
    assert client.post("/api/lessons", json={"pack_id": dds, "source": "student"}, headers=th).status_code == 409
    assert client.post("/api/lessons", json={"pack_id": dds, "source": "всё"}, headers=th).status_code == 400

    sc = client.post("/api/scenarios/generate", json={"mode": "cards", "groups": ["14"], "count": 1, "seed": 2},
                     headers=th).json()
    client.post("/api/scenarios/%d/approve" % sc["id"], json={}, headers=th)
    L = client.post("/api/lessons", json={"pack_id": sc["slug"], "count": 1}, headers=th).json()["id"]
    client.post("/api/lessons/%s/join" % L, headers=sh)
    client.post("/api/lessons/%s/roles" % L, json={"participant": sid, "service": "Оператор 112"}, headers=th)
    with client.websocket_connect("/ws?lesson=" + L) as ws:
        hello(ws, stok)
        recv(ws, "state")
        client.post("/api/lessons/%s/start" % L, headers=th)
        clock.now += 3
        call = recv(ws, "incoming")["call"]
        ws.send_json({"t": "answer"})
        recv(ws, "caller_say")
        clock.now += 20
        spec = engine.get(L).pack.by_key[engine.get(L).desk.by_number[call["number"]]]
        ws.send_json({"t": "save_card", "card": perfect_card(spec, call["number"]), "seq": 1})
        assert recv(ws, "card_result")["result"]["score"] == 100
        client.post("/api/lessons/%s/finish" % L, headers=th)
    assert db.scalar("SELECT COUNT(*) AS n FROM student_cards") == 1
    packs = {p["id"]: p for p in client.get("/api/packs", headers=th).json()}
    assert packs[dds]["student_cards"] == 1

    only = client.post("/api/lessons", json={"pack_id": dds, "source": "student", "count": 5}, headers=th).json()
    assert only["pack"]["cards"] == 1 and only["config"]["source"] == "student"
    card = engine.get(only["id"]).pack.cards[0]
    assert card["key"] == "S1" and card["code"] == spec["gold"]["codes"][0]
    mixed = client.post("/api/lessons", json={"pack_id": dds, "source": "mixed", "count": 5}, headers=th).json()
    assert mixed["pack"]["cards"] == len(engine.packs[dds].cards) + 1
