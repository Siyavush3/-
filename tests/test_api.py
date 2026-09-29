"""HTTP и WebSocket: занятие через настоящий транспорт, права ролей, аудит, перезапуск сервера."""
import json
import os

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.auth import Accounts
from app.main import app as default_app, create_app, load_engine
from arm112.paths import data_dir
from arm112.reference.fsm import ResponseFsm
from arm112.reference.territory import Territory
from domain.engine import Engine
from domain.evaluation import Rubric
from domain.scenario import CardViews, Pack, Router
from store.db import Database
from store.schema import migrate

PASSWORDS = {"admin": "Admin-112", "teacher": "Teacher-112", "student": "Student-112"}
USERS = [("admin", "Админ", "admin"), ("t1", "Петрова", "teacher"), ("t2", "Козлов", "teacher"),
         ("s1", "Иванов", "student"), ("s2", "Соколова", "student")]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_engine(classifier):
    with open(data_dir() / "classifier" / "ui_services.json", encoding="utf-8") as f:
        router = Router(classifier, Territory.load(), json.load(f))
    pack = Pack.load(data_dir() / "scenarios" / "dds_shchukino_v0.json")
    clock = Clock()
    engine = Engine(router, CardViews.load(classifier), ResponseFsm.load(), Rubric.load(),
                    {pack.id: pack}, clock=clock, wall=lambda: 1_758_000_000.0)
    return engine, clock, pack


def make_db(path):
    db = Database("sqlite:///" + str(path))
    migrate(db)
    accounts = Accounts(db)
    ids = {}
    for login, name, role in USERS:
        if not accounts.by_login(login):
            user, _ = accounts.create(login, name, role, PASSWORDS[role], must_change=False)
            ids[login] = user.id
        else:
            ids[login] = accounts.by_login(login).id
    return db, ids


@pytest.fixture
def world(classifier, tmp_path):
    engine, clock, pack = make_engine(classifier)
    db, ids = make_db(tmp_path / "api.sqlite")
    return engine, clock, pack, db, ids


@pytest.fixture
def env(world):
    engine, clock, pack, db, ids = world
    with TestClient(create_app(engine, db, tick_s=0.01)) as client:
        yield client, clock, pack, ids


def login(client, login_, role=None):
    role = role or next(r for l, _, r in USERS if l == login_)
    res = client.post("/api/login", json={"login": login_, "password": PASSWORDS[role]})
    assert res.status_code == 200, res.text
    body = res.json()
    return {"Authorization": "Bearer " + body["token"]}, body["token"], "u%d" % body["user"]["id"]


def recv(ws, kind, limit=200):
    """Читать сокет до сообщения нужного типа (попутные — обновления ботов и т.п.)."""
    seen = []
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["t"] == kind:
            return msg
        seen.append(msg["t"])
    raise AssertionError("нет «%s», пришли: %s" % (kind, seen))


def hello(ws, token):
    ws.send_json({"t": "hello", "token": token})


def test_health_reports_core_state():
    body = TestClient(default_app).get("/api/health").json()
    assert body["status"] == "ok"
    assert set(body["core"]) >= {"loaded"}
    if os.environ.get("A112_REQUIRE_NATIVE") == "1":
        assert body["core"]["loaded"] is True
        assert body["core"]["version"] == body["version"]


def test_load_engine_routes_through_native_core(native):
    engine = load_engine()
    assert engine.router.engine_name == "libarm112 (C)"
    assert "dds-shchukino-v0" in engine.packs


def test_login_errors_and_audit(env):
    client, clock, pack, ids = env
    assert client.post("/api/login", json={"login": "s1", "password": "wrong-1"}).status_code == 401
    assert client.get("/api/me").status_code == 401
    ah, _, _ = login(client, "admin")
    actions = [r["action"] for r in client.get("/api/admin/audit", headers=ah).json()]
    assert actions[:2] == ["login.ok", "login.fail"]


def test_rbac_matrix(env):
    """Каждая роль видит только своё (ТЗ: разграничение доступа по ролям)."""
    client, clock, pack, ids = env
    heads = {r: login(client, l)[0] for l, r in [("admin", "admin"), ("t1", "teacher"), ("s1", "student")]}
    matrix = [  # (метод, путь, тело, {роль: ожидаемый код})
        ("get", "/api/packs", None, {"admin": 403, "teacher": 200, "student": 403}),
        ("get", "/api/lessons", None, {"admin": 403, "teacher": 200, "student": 200}),
        ("post", "/api/lessons", {"pack_id": pack.id}, {"admin": 403, "student": 403}),
        ("get", "/api/admin/users", None, {"admin": 200, "teacher": 403, "student": 403}),
        ("get", "/api/admin/audit", None, {"admin": 200, "teacher": 403, "student": 403}),
        ("get", "/api/results/me", None, {"admin": 403, "teacher": 403, "student": 200}),
        ("get", "/api/results", None, {"admin": 403, "teacher": 200, "student": 403}),
    ]
    for method, path, body, expected in matrix:
        for role, code in expected.items():
            res = getattr(client, method)(path, headers=heads[role], **({"json": body} if body else {}))
            assert res.status_code == code, (method, path, role, res.status_code, res.text)


def test_full_lesson_over_http_and_websocket(env):
    client, clock, pack, ids = env
    th, _, _ = login(client, "t1")
    sh, stok, sid = login(client, "s1")

    L = client.post("/api/lessons", json={"pack_id": pack.id}, headers=th).json()["id"]
    assert client.post("/api/lessons/%s/join" % L, headers=sh).status_code == 200
    wrong = client.post("/api/lessons/%s/roles" % L, json={"participant": sid, "service": "Мослифт"}, headers=th)
    assert wrong.status_code == 409  # сценарий написан для одной роли
    client.post("/api/lessons/%s/roles" % L, json={"participant": sid, "service": pack.role}, headers=th)

    with client.websocket_connect("/ws?lesson=" + L) as ws:
        hello(ws, stok)
        state = recv(ws, "state")
        assert state["lesson"]["role"] == pack.role and state["cards"] == [] and state["last_seq"] == 0
        assert client.post("/api/lessons/%s/start" % L, headers=th).status_code == 200
        assert recv(ws, "state")["lesson"]["state"] == "идёт"

        clock.now += 10                                   # бот-оператор 112 выдаёт карточку I
        card = recv(ws, "card")["card"]
        assert card["own_service"] == pack.role
        assert "expect" not in card and "brigade" not in card  # эталон не уходит на рабочее место
        ws.send_json({"t": "open_card", "card": card["number"]})
        assert recv(ws, "card_status")["entry"]["status"] == "Получена службой"
        ws.send_json({"t": "set_status", "card": card["number"], "status": "Принята",
                      "comment": "направлены сантехник и электрик", "seq": 1})
        assert recv(ws, "status_ack")["seq"] == 1
        update = recv(ws, "card_status")
        assert update["entry"]["status"] == "Принята" and "Начало реагирования" in update["allowed"]
        ws.send_text("не json")
        assert recv(ws, "error")

    # Переподключение: снимок продолжает нумерацию команд, повтор команды отбрасывается.
    with client.websocket_connect("/ws?lesson=" + L) as ws:
        hello(ws, stok)
        state = recv(ws, "state")
        assert state["last_seq"] == 1
        assert state["cards"][0]["statuses"][pack.role][-1]["status"] == "Принята"
        ws.send_json({"t": "set_status", "card": card["number"], "status": "Принята", "seq": 1})
        assert recv(ws, "status_ack")["duplicate"] is True

        clock.now += 45                                   # старший бригады докладывает о выезде
        call = recv(ws, "call")
        assert call["card"] == card["number"] and call["from"] == "Старший бригады"

        assert client.post("/api/lessons/%s/finish" % L, headers=th).status_code == 200
        result = recv(ws, "result")["result"]
    assert result["participant"] == sid and len(result["categories"]) == 8  # 7 категорий памятки + ошибки в тексте

    own = client.get("/api/lessons/%s/results" % L, headers=sh).json()
    assert own["participant"] == sid
    full = client.get("/api/lessons/%s/results" % L, headers=th).json()
    assert [p["participant"] for p in full["participants"]] == [sid]
    history = client.get("/api/results/me", headers=sh).json()
    assert [h["lesson"] for h in history] == [L]


def test_results_survive_restart_and_stay_private(classifier, tmp_path):
    """Результаты в базе: после перезапуска сервера они на месте, чужие — недоступны (ТЗ)."""
    engine, clock, pack = make_engine(classifier)
    db, ids = make_db(tmp_path / "restart.sqlite")
    with TestClient(create_app(engine, db, tick_s=0.01)) as client:
        th, _, _ = login(client, "t1")
        sh, _, sid = login(client, "s1")
        L = client.post("/api/lessons", json={"pack_id": pack.id}, headers=th).json()["id"]
        client.post("/api/lessons/%s/join" % L, headers=sh)
        client.post("/api/lessons/%s/roles" % L, json={"participant": sid, "service": pack.role}, headers=th)
        client.post("/api/lessons/%s/start" % L, headers=th)
        clock.now += 20
        client.post("/api/lessons/%s/finish" % L, headers=th)
        L2 = client.post("/api/lessons", json={"pack_id": pack.id}, headers=th).json()["id"]  # остался в наборе
    db.close()

    engine2, _, _ = make_engine(classifier)
    db2 = Database("sqlite:///" + str(tmp_path / "restart.sqlite"))
    with TestClient(create_app(engine2, db2, tick_s=0.01)) as client:
        th, _, _ = login(client, "t1")
        states = {l["id"]: l["state"] for l in client.get("/api/lessons", headers=th).json()}
        assert states == {L: "отчёт", L2: "прервано"}
        results = client.get("/api/lessons/%s/results" % L, headers=th).json()
        assert [p["participant"] for p in results["participants"]] == [sid]
        assert results["participants"][0]["findings"]                 # по карточке I статуса нет — замечание
        sh, _, _ = login(client, "s1")
        assert len(client.get("/api/results/me", headers=sh).json()) == 1
        s2h, _, _ = login(client, "s2")
        assert client.get("/api/results/me", headers=s2h).json() == []
        assert client.get("/api/lessons/%s/results" % L, headers=s2h).status_code == 403
        t2h, _, _ = login(client, "t2")
        assert client.get("/api/lessons/%s/results" % L, headers=t2h).status_code == 403
        new = client.post("/api/lessons", json={"pack_id": pack.id}, headers=th).json()["id"]
        assert new not in (L, L2)                                     # номера занятий не повторяются
        events = db2.query("SELECT kind FROM lesson_events WHERE lesson_id = ?", (int(L[1:]),))
        assert {"start", "card", "finish"} <= {e["kind"] for e in events}


def test_admin_manages_accounts_with_stepup(env):
    client, clock, pack, ids = env
    ah, atok, aid = login(client, "admin")
    new = {"login": "s9", "name": "Новый Ученик", "role": "student"}
    assert client.post("/api/admin/users", json=new, headers=ah).status_code == 428   # нужно подтверждение
    assert client.post("/api/admin/confirm", json={"password": "wrong-1"}, headers=ah).status_code == 403
    assert client.post("/api/admin/confirm", json={"password": PASSWORDS["admin"]}, headers=ah).status_code == 200
    created = client.post("/api/admin/users", json=new, headers=ah).json()
    temp = created["temporary_password"]

    res = client.post("/api/login", json={"login": "s9", "password": temp}).json()
    assert res["user"]["must_change"] is True
    s9 = {"Authorization": "Bearer " + res["token"]}
    assert client.get("/api/lessons", headers=s9).status_code == 403                  # сначала сменить пароль
    assert client.post("/api/password", json={"old": temp, "new": "Mine-12345"}, headers=s9).status_code == 200
    assert client.get("/api/lessons", headers=s9).status_code == 200

    uid = created["user"]["id"]
    assert client.patch("/api/admin/users/%d" % uid, json={"blocked": True}, headers=ah).status_code == 200
    assert client.get("/api/lessons", headers=s9).status_code == 401                  # сеанс отозван
    assert client.post("/api/login", json={"login": "s9", "password": "Mine-12345"}).status_code == 401
    assert client.patch("/api/admin/users/%d" % int(aid[1:]), json={"blocked": True}, headers=ah).status_code == 409
    temp2 = client.post("/api/admin/users/%d/reset-password" % uid, headers=ah).json()["temporary_password"]
    client.patch("/api/admin/users/%d" % uid, json={"blocked": False}, headers=ah)
    assert client.post("/api/login", json={"login": "s9", "password": temp2}).status_code == 200

    actions = {r["action"] for r in client.get("/api/admin/audit?limit=100", headers=ah).json()}
    assert {"user.create", "user.block", "user.unblock", "user.reset_password", "password.change",
            "admin.confirm", "admin.confirm_fail"} <= actions
    assert client.get("/api/admin/audit/verify", headers=ah).json()["ok"] is True


def test_admin_cannot_join_lessons(env):
    client, clock, pack, ids = env
    th, _, _ = login(client, "t1")
    L = client.post("/api/lessons", json={"pack_id": pack.id}, headers=th).json()["id"]
    _, atok, _ = login(client, "admin")
    with client.websocket_connect("/ws?lesson=" + L) as ws:
        hello(ws, atok)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4403


def test_teacher_monitor_over_websocket(env):
    client, clock, pack, ids = env
    th, ttok, _ = login(client, "t1")
    L = client.post("/api/lessons", json={"pack_id": pack.id}, headers=th).json()["id"]
    with client.websocket_connect("/ws?lesson=" + L) as ws:
        hello(ws, ttok)
        assert recv(ws, "monitor")["participants"] == []
        sh, _, sid = login(client, "s1")
        client.post("/api/lessons/%s/join" % L, headers=sh)
        rows = recv(ws, "monitor")["participants"]
        assert [(r["participant"], r["role"]) for r in rows] == [(sid, None)]


def test_websocket_rejects_strangers(env):
    client, clock, pack, ids = env
    th, _, _ = login(client, "t1")
    L = client.post("/api/lessons", json={"pack_id": pack.id}, headers=th).json()["id"]
    _, stok, _ = login(client, "s2")
    _, other_ttok, _ = login(client, "t2")
    cases = [({"t": "hello", "token": "чужой"}, L, 4401),
             ({"t": "hello", "token": stok}, L, 4403),          # не вошёл в занятие
             ({"t": "hello", "token": other_ttok}, L, 4403),    # чужое занятие
             ({"t": "hello", "token": stok}, "L999", 4404)]
    for first, lesson, code in cases:
        with client.websocket_connect("/ws?lesson=" + lesson) as ws:
            ws.send_json(first)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == code


def test_rest_errors_map_to_status_codes(env):
    client, clock, pack, ids = env
    assert client.get("/api/packs").status_code == 401
    th, _, _ = login(client, "t1")
    assert client.post("/api/lessons", json={"pack_id": "нет-такого"}, headers=th).status_code == 404
    assert client.post("/api/lessons", json={"pack_id": pack.id, "time_scale": 5}, headers=th).status_code == 400
    slow = client.post("/api/lessons", json={"pack_id": pack.id, "time_scale": 3}, headers=th)   # замедление — можно
    assert slow.status_code == 200 and slow.json()["time_scale"] == 3
    L = client.post("/api/lessons", json={"pack_id": pack.id}, headers=th).json()["id"]
    assert client.post("/api/lessons/%s/start" % L, headers=th).status_code == 409   # никого нет
    assert client.get("/api/lessons/%s/results" % L, headers=th).status_code == 409  # ещё не завершено
    assert client.get("/api/lessons/Lxx/results", headers=th).status_code == 404
    assert [p["id"] for p in client.get("/api/packs", headers=th).json()] == [pack.id]


def test_real_server_speaks_websocket(world):
    """Настоящий uvicorn и настоящий клиент: TestClient не заметил бы, что у сервера нет WebSocket."""
    import socket
    import threading
    import time
    import urllib.request

    import uvicorn
    from websockets.sync.client import connect

    engine, clock, pack, db, ids = world
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = uvicorn.Server(uvicorn.Config(create_app(engine, db, tick_s=0.05), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "сервер не поднялся"
        time.sleep(0.05)

    def post(path, body, token=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    try:
        teacher = post("/api/login", {"login": "t1", "password": PASSWORDS["teacher"]})
        lesson = post("/api/lessons", {"pack_id": pack.id}, teacher["token"])
        with connect("ws://127.0.0.1:%d/ws?lesson=%s" % (port, lesson["id"]), open_timeout=5) as ws:
            ws.send(json.dumps({"t": "hello", "token": teacher["token"]}))
            assert json.loads(ws.recv(timeout=5))["t"] == "monitor"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def make_cards_world(classifier, tmp_path):
    from domain.calls import CallPack
    engine, clock, pack = make_engine(classifier)
    calls = CallPack.load(data_dir() / "scenarios" / "calls_bilety.json")
    calls.validate(classifier)
    engine.packs[calls.id] = calls
    db, ids = make_db(tmp_path / "cards.sqlite")
    return engine, clock, calls, db


def test_cards_lesson_over_http_and_websocket(classifier, tmp_path):
    """Режим «карточки» целиком: вызов → разговор → карточка → разбор → итог (узел 2)."""
    from tests_support import perfect_card
    engine, clock, calls, db = make_cards_world(classifier, tmp_path)
    with TestClient(create_app(engine, db, tick_s=0.01)) as client:
        th, _, _ = login(client, "t1")
        sh, stok, sid = login(client, "s1")
        packs = {p["id"]: p for p in client.get("/api/packs", headers=th).json()}
        assert packs[calls.id]["mode"] == "cards" and packs[calls.id]["groups"]
        bad = client.post("/api/lessons", json={"pack_id": calls.id, "count": 0}, headers=th)
        assert bad.status_code == 400
        lesson = client.post("/api/lessons", json={"pack_id": calls.id, "count": 1, "norm_s": 120}, headers=th).json()
        L = lesson["id"]
        assert lesson["mode"] == "cards" and lesson["config"]["count"] == 1
        client.post("/api/lessons/%s/join" % L, headers=sh)
        client.post("/api/lessons/%s/roles" % L, json={"participant": sid, "service": "Оператор 112"}, headers=th)
        with client.websocket_connect("/ws?lesson=" + L) as ws:
            hello(ws, stok)
            assert recv(ws, "state")["lesson"]["mode"] == "cards"
            client.post("/api/lessons/%s/start" % L, headers=th)
            clock.now += 3
            call = recv(ws, "incoming")["call"]
            key = engine.get(L).desk.by_number[call["number"]]
            ws.send_json({"t": "answer"})
            assert recv(ws, "caller_say")["line"]["text"].startswith("Алло")
            ws.send_json({"t": "ask", "intent": "name", "text": "Как вас зовут?"})
            assert "зовут" in recv(ws, "caller_say")["line"]["text"]
            clock.now += 30
            ws.send_json({"t": "save_card", "card": perfect_card(calls.by_key[key], call["number"]), "seq": 1})
            result = recv(ws, "card_result")["result"]
            assert result["score"] == 100 and result["gold"]["code"] == calls.by_key[key]["gold"]["codes"][0]
            assert recv(ws, "queue_done")
            client.post("/api/lessons/%s/finish" % L, headers=th)
            final = recv(ws, "result")["result"]
        assert final["score"] == 100 and final["cards"] == 1 and final["timing"]["norm_s"] == 120
        stored = client.get("/api/lessons/%s/results" % L, headers=th).json()
        assert stored["participants"][0]["score"] == 100 and stored["participants"][0]["timing"]["avg_fill_s"] == 30
        kinds = {r["kind"] for r in db.query("SELECT kind FROM lesson_events WHERE lesson_id = ?", (int(L[1:]),))}
        assert {"start", "ring", "answer", "ask", "save", "finish"} <= kinds


def test_classifier_endpoints(classifier, tmp_path):
    engine, clock, calls, db = make_cards_world(classifier, tmp_path)
    with TestClient(create_app(engine, db, tick_s=0.01)) as client:
        sh, _, _ = login(client, "s1")
        types = client.get("/api/classifier/call-types", headers=sh).json()
        assert len(types) == 51 and next(t for t in types if t["name"] == "101")["group"] == "1"
        assert next(t for t in types if t["name"] == "Консультация")["group"] is None
        group = client.get("/api/classifier/groups/1", headers=sh).json()
        assert group["title"] == "Происшествие 101" and len(group["incidents"]) == 127
        found = [x["code"] for x in client.get("/api/classifier/search?q=мусоропровод дым", headers=sh).json()]
        assert found == ["1050602"]
        route = client.get("/api/route?code=1050602", headers=sh).json()
        assert {"name": "Служба 101", "row": 1, "main": True, "manual": False} in route
        assert client.get("/api/route?code=0", headers=sh).status_code == 404
        assert len(client.get("/api/services", headers=sh).json()) == 211
