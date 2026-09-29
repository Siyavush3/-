"""Преподаватель и обучающийся (узел 6): группы, задания, самостоятельное выполнение, критерии и нормативы,
экспертная оценка с аудитом, отчёт о занятии и выгрузки, аналитика и прогноз, материалы, рекомендации."""
import io
import json

import pytest
from fastapi.testclient import TestClient

from domain import analytics
from store.repos import Lessons, Results
from test_api import hello, login, make_db, make_engine, recv
from tests_support import perfect_card


@pytest.fixture
def app_env(classifier, tmp_path, monkeypatch):
    from app.main import create_app
    monkeypatch.setenv("A112_MATERIALS_DIR", str(tmp_path / "materials"))
    engine, clock, pack = make_engine(classifier)
    db, ids = make_db(tmp_path / "teach.sqlite")
    with TestClient(create_app(engine, db, tick_s=0.01)) as client:
        yield client, engine, clock, db, ids


def group_with(client, headers, name, member_ids):
    g = client.post("/api/groups", json={"name": name}, headers=headers).json()
    return client.patch("/api/groups/%d" % g["id"], json={"members": member_ids}, headers=headers).json()


# ---------- группы ----------
def test_groups_belong_to_their_teacher(app_env):
    client, engine, clock, db, ids = app_env
    th, _, _ = login(client, "t1")
    t2h, _, _ = login(client, "t2")
    sh, _, _ = login(client, "s1")
    g = group_with(client, th, "ДДС-1", [ids["s1"], ids["s2"]])
    assert [m["login"] for m in g["members"]] == ["s1", "s2"]
    assert client.get("/api/groups", headers=t2h).json() == []                       # чужих групп не видно
    assert client.patch("/api/groups/%d" % g["id"], json={"name": "x"}, headers=t2h).status_code == 404
    assert client.patch("/api/groups/%d" % g["id"], json={"members": [ids["t2"]]}, headers=th).status_code == 400
    assert [x["name"] for x in client.get("/api/me/groups", headers=sh).json()] == ["ДДС-1"]
    assert client.get("/api/groups", headers=sh).status_code == 403
    assert client.delete("/api/groups/%d" % g["id"], headers=th).json()["ok"]
    assert client.get("/api/me/groups", headers=sh).json() == []


def test_group_lesson_is_only_for_its_members(app_env):
    client, engine, clock, db, ids = app_env
    th, _, _ = login(client, "t1")
    s1h, _, _ = login(client, "s1")
    s2h, _, _ = login(client, "s2")
    g = group_with(client, th, "Группа 1", [ids["s1"]])
    lesson = client.post("/api/lessons", json={"pack_id": "dds-shchukino-v0", "group_id": g["id"]}, headers=th).json()
    assert [l["id"] for l in client.get("/api/lessons", headers=s1h).json()] == [lesson["id"]]
    assert client.get("/api/lessons", headers=s2h).json() == []
    assert client.post("/api/lessons/%s/join" % lesson["id"], headers=s2h).status_code == 403
    assert client.post("/api/lessons/%s/join" % lesson["id"], headers=s1h).status_code == 200


# ---------- задания и самостоятельное выполнение ----------
def test_task_is_done_by_student_alone(app_env):
    """Преподаватель назначает группе задание, обучающийся сам запускает его, проходит и завершает."""
    client, engine, clock, db, ids = app_env
    th, _, _ = login(client, "t1")
    sh, stok, sid = login(client, "s1")
    s2h, _, _ = login(client, "s2")
    g = group_with(client, th, "112-1", [ids["s1"]])
    sc = client.post("/api/scenarios/generate", json={"mode": "cards", "groups": ["14"], "count": 2, "seed": 3},
                     headers=th).json()
    client.post("/api/scenarios/%d/approve" % sc["id"], json={}, headers=th)
    bad = client.post("/api/tasks", json={"group_id": g["id"], "pack_id": sc["slug"], "lesson": {"pass_score": 150}},
                      headers=th)
    assert bad.status_code == 400
    task = client.post("/api/tasks", json={"group_id": g["id"], "pack_id": sc["slug"], "title": "Вода и лифты",
                                           "lesson": {"count": 1, "pass_score": 80}}, headers=th).json()
    assert task["mode"] == "cards" and task["difficulty"] == 1 and task["attempts"] == []
    mine = client.get("/api/tasks", headers=sh).json()
    assert [t["title"] for t in mine] == ["Вода и лифты"] and mine[0]["active_lesson"] is None
    assert client.get("/api/tasks", headers=s2h).json() == []                         # задание не его группы
    assert client.post("/api/tasks/%d/start" % task["id"], headers=s2h).status_code == 404

    started = client.post("/api/tasks/%d/start" % task["id"], headers=sh).json()
    L = started["id"]
    assert started["page"] == "op112.html" and started["state"] == "идёт" and started["config"]["solo"]
    assert client.post("/api/tasks/%d/start" % task["id"], headers=sh).json()["id"] == L   # повторно — то же занятие
    assert L in [l["id"] for l in client.get("/api/lessons", headers=th).json()]          # преподаватель видит
    with client.websocket_connect("/ws?lesson=" + L) as ws:
        hello(ws, stok)
        recv(ws, "state")
        clock.now += 5
        call = recv(ws, "incoming")["call"]
        ws.send_json({"t": "answer"})
        recv(ws, "caller_say")
        clock.now += 20
        spec = engine.get(L).pack.by_key[engine.get(L).desk.by_number[call["number"]]]
        ws.send_json({"t": "save_card", "card": perfect_card(spec, call["number"]), "seq": 1})
        assert recv(ws, "card_result")["result"]["score"] == 100
    assert client.post("/api/lessons/%s/finish" % L, headers=s2h).status_code == 403    # чужое не завершить
    assert client.post("/api/lessons/%s/finish" % L, headers=sh).status_code == 200
    mine = client.get("/api/tasks", headers=sh).json()[0]
    assert mine["passed"] and mine["best"] == 100 and mine["attempts"][0]["lesson"] == L
    assert client.get("/api/tasks", headers=th).json()[0]["attempts"][0]["student"] == "Иванов"


# ---------- критерии, нормативы, экспертная оценка ----------
def dds_lesson_with_student(client, engine, clock, th, sh, sid, config=None):
    body = dict({"pack_id": "dds-shchukino-v0", "time_scale": 1.0}, **(config or {}))
    L = client.post("/api/lessons", json=body, headers=th).json()["id"]
    client.post("/api/lessons/%s/join" % L, headers=sh)
    client.post("/api/lessons/%s/roles" % L, json={"participant": sid, "service": "Поселение Щукино"}, headers=th)
    client.post("/api/lessons/%s/start" % L, headers=th)
    return L


def act(engine, clock, L, sid, t, key, status, comment=""):
    rt = engine.get(L)
    clock.now = rt.lesson.started_at + t
    engine.tick()
    return engine.handle(L, sid, {"t": "set_status", "card": rt.numbers[key], "status": status, "comment": comment,
                                  "seq": int(t * 10)})


def test_norm_and_criteria_are_set_by_teacher(app_env):
    client, engine, clock, db, ids = app_env
    th, _, _ = login(client, "t1")
    sh, _, sid = login(client, "s1")
    strict = dds_lesson_with_student(client, engine, clock, th, sh, sid,
                                     {"primary_norm_s": 10, "pass_score": 99, "max_errors": 0})
    act(engine, clock, strict, sid, 32, "I", "Принята", "принята")                  # карточка I пришла на 10-й секунде
    res = client.post("/api/lessons/%s/finish" % strict, headers=th).json()
    assert res["config"]["primary_norm_s"] == 10
    r = client.get("/api/lessons/%s/results" % strict, headers=th).json()["participants"][0]
    assert any(f["category"] == 1 and "нормативе 10" in f["detail"] for f in r["findings"])
    assert r["passed"] is False and any("ниже порога 99" in x for x in r["reasons"])
    assert any("допустимых 0" in x for x in r["reasons"])
    default = dds_lesson_with_student(client, engine, clock, th, sh, sid)
    act(engine, clock, default, sid, 32, "I", "Принята", "принята")
    client.post("/api/lessons/%s/finish" % default, headers=th)
    r = client.get("/api/lessons/%s/results" % default, headers=th).json()["participants"][0]
    assert not any(f["category"] == 1 and "статус через" in f["detail"] for f in r["findings"])   # 22 с < 30 с
    assert r["criteria"] == {"pass_score": 70, "max_errors": None, "text_check": True}


def test_expert_assessment_is_audited(app_env):
    client, engine, clock, db, ids = app_env
    th, _, _ = login(client, "t1")
    t2h, _, _ = login(client, "t2")
    sh, _, sid = login(client, "s1")
    L = dds_lesson_with_student(client, engine, clock, th, sh, sid)
    act(engine, clock, L, sid, 12, "I", "Принята", "принята")
    client.post("/api/lessons/%s/finish" % L, headers=th)
    rid = client.get("/api/lessons/%s/results" % L, headers=th).json()["participants"][0]["result_id"]
    assert client.put("/api/results/%d/expert" % rid, json={"score": 150}, headers=th).status_code == 400
    assert client.put("/api/results/%d/expert" % rid, json={"score": 40}, headers=t2h).status_code == 404
    res = client.put("/api/results/%d/expert" % rid, json={"score": 55, "comment": "Не отмечен ход работ"},
                     headers=th).json()
    assert res["final_score"] == 55 and res["passed"] is False
    row = db.one("SELECT details FROM audit WHERE action = 'result.expert'")
    details = json.loads(row["details"])
    assert details["before"] is None and details["after"] == 55 and details["student"] == "s1"
    me = client.get("/api/results/me", headers=sh).json()[0]
    assert me["expert_score"] == 55 and me["expert_comment"] == "Не отмечен ход работ"
    stored = client.get("/api/lessons/%s/results" % L, headers=th).json()["participants"][0]
    assert stored["final_score"] == 55 and stored["score"] != 55


# ---------- отчёт о занятии ----------
def test_lesson_report_with_actions_timing_and_exports(app_env):
    client, engine, clock, db, ids = app_env
    th, _, _ = login(client, "t1")
    sh, _, sid = login(client, "s1")
    L = dds_lesson_with_student(client, engine, clock, th, sh, sid)
    act(engine, clock, L, sid, 15, "I", "Принята", "принята, направлен сантехник")
    act(engine, clock, L, sid, 120, "A", "Не принята", "дом не наш")                  # A пришла на 70-й: 50 с > 30
    client.post("/api/lessons/%s/finish" % L, headers=th)
    rep = client.get("/api/lessons/%s/report" % L, headers=th).json()
    p = rep["participants"][0]
    cards = {c["key"]: c for c in p["cards"]}
    assert cards["I"]["primary_s"] == 5.0 and cards["I"]["over_s"] is None
    assert cards["A"]["primary_s"] == 50.0 and cards["A"]["over_s"] == 20.0
    assert cards["I"]["actions"][0]["what"] == "Принята" and cards["I"]["actions"][0]["comment"].startswith("принята")
    assert p["timing"]["norm_s"] == 30 and p["timing"]["over_norm"] == 1 and rep["elapsed_ms"] < 30000
    csv = client.get("/api/lessons/%s/report.csv" % L, headers=th)
    text = csv.content.decode("utf-8")
    assert text.startswith("﻿Обучающийся;Итоговый балл") and "Иванов" in text
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(client.get("/api/lessons/%s/report.xlsx" % L, headers=th).content))
    assert wb.sheetnames == ["Итоги", "Карточки"] and wb["Итоги"]["A2"].value == "Иванов"
    own = client.get("/api/lessons/%s/report" % L, headers=sh).json()                   # обучающемуся — своя часть
    assert [x["name"] for x in own["participants"]] == ["Иванов"]
    assert client.get("/api/lessons/%s/report.csv" % L, headers=sh).status_code == 403


# ---------- аналитика и прогноз ----------
def learning_curves(students=300, lessons=8, seed=7, noise=(3, 8)):
    """Кривые обучения: приближение к потолку с разной скоростью; шум 3–8 баллов (балл — среднее по карточкам)."""
    import math
    import random
    rng = random.Random(seed)
    out = []
    for _ in range(students):
        s0, k, sd, cap = rng.uniform(30, 75), rng.uniform(0.0, 0.45), rng.uniform(*noise), rng.uniform(80, 100)
        out.append([max(0.0, min(100.0, cap - (cap - s0) * math.exp(-k * t) + rng.gauss(0, sd))) for t in range(lessons)])
    return out


def test_forecast_beats_naive_on_learning_curves():
    """Достоверность прогноза: на истории каждое занятие предсказано по предыдущим; ошибка меньше,
    чем у прогноза «как в прошлый раз»; 80% интервал накрывает факт примерно в 80% случаев."""
    curves = learning_curves()
    series = dict(enumerate(curves))
    points = [p for sid, ys in series.items() for p in analytics.backtest(ys, analytics.group_step(series, sid))]
    m = analytics.metrics(points, 70)
    assert m["n"] == 300 * 7 and m["mae"] < m["mae_naive"] * 0.92 and abs(m["bias"]) < 2
    noisy = dict(enumerate(learning_curves(seed=11, noise=(4, 12))))                  # и при шуме посильнее — лучше наивного
    mn = analytics.metrics([p for sid, ys in noisy.items() for p in analytics.backtest(ys, analytics.group_step(noisy, sid))], 70)
    assert mn["mae"] < mn["mae_naive"] * 0.95
    sigma = m["rmse"]
    covered = sum(abs(p["actual"] - p["predicted"]) <= analytics.Z80 * sigma for p in points) / len(points)
    assert 0.75 <= covered <= 0.9


def test_forecast_math():
    assert analytics.predict([80, 80, 80, 80]) == pytest.approx(80, abs=0.01)
    rising = [50, 60, 70, 80]
    assert analytics.predict(rising, group_step=10) > 80                              # группа растёт — и прогноз
    assert analytics.predict([60], group_step=5) == pytest.approx(64.5)               # одно занятие — прирост группы
    assert analytics.predict([50, 65, 80], group_step=-1) > 78                         # явный личный рост важнее группы
    assert analytics.predict([100, 100, 100], group_step=10) == 100                   # не больше 100
    bt = analytics.backtest(rising, 0.0)
    assert [p["actual"] for p in bt] == [60, 70, 80] and all(p["predicted"] < p["actual"] for p in bt)
    m = analytics.metrics(bt, 70)
    assert m["n"] == 3 and m["mae"] > 0 and m["mae_naive"] == 10.0
    steady = [40, 45, 50, 55, 60, 65, 70, 75]
    m = analytics.metrics(analytics.backtest(steady, 5.0), 70)
    assert m["mae"] < m["mae_naive"]                                                  # лучше наивного на тренде
    f = analytics.forecast(steady, 5.0, 6.0, 70)
    assert f["next"] >= 75 and f["p_pass"] > 0.8 and f["lessons_to_pass"] == 1 and f["low"] <= f["next"] <= f["high"]
    slow = analytics.forecast([40, 42, 44], 2.0, 6.0, 70)
    assert slow["p_pass"] < 0.1 and slow["lessons_to_pass"] and slow["lessons_to_pass"] > 3
    assert analytics.forecast([], 0, 10, 70) is None


def seed_results(db, ids, clock_start=1000.0):
    """Три занятия ДДС для s1 (растёт) и s2 (стоит, одна и та же ошибка №1)."""
    t = [clock_start]

    def clock():
        t[0] += 60
        return t[0]
    lessons, results = Lessons(db, clock), Results(db, clock)
    cats = [{"category": 1, "title": "Отсутствует статус реагирования", "weight": 10},
            {"category": 5, "title": "Внесён неполный комментарий", "weight": 5}]
    for i, (s1, s2) in enumerate([(50, 60), (65, 60), (80, 58)]):
        lid = lessons.create(ids["t1"], "действия с карточками", "dds-shchukino-v0", "Щукино %d" % i, {}, "набор")
        lessons.set_state(lid, "завершено", started_at=clock(), finished_at=clock())
        results.save(lid, [
            {"participant": "u%d" % ids["s1"], "role": "ДДС", "score": s1, "cards": 10, "categories": cats,
             "findings": [{"category": 1, "title": "x", "card": "1", "detail": "статус через 40 с"}] * (2 - min(i, 2))},
            {"participant": "u%d" % ids["s2"], "role": "ДДС", "score": s2, "cards": 10, "categories": cats,
             "findings": [{"category": 1, "title": "x", "card": "1", "detail": "статус через 50 с"},
                          {"category": 8, "title": "x", "card": "2", "detail": "«информацыю» → информацию"}]}])


def test_analytics_api_forecast_heatmap_insights(app_env):
    client, engine, clock, db, ids = app_env
    th, _, _ = login(client, "t1")
    t2h, _, _ = login(client, "t2")
    sh, _, _ = login(client, "s1")
    g = group_with(client, th, "Аналитика", [ids["s1"], ids["s2"]])
    seed_results(db, ids)
    a = client.get("/api/analytics?group_id=%d" % g["id"], headers=th).json()
    by = {s["name"]: s for s in a["students"]}
    ivanov, sokolova = by["Иванов"], by["Соколова"]
    assert [h["score"] for h in ivanov["history"]] == [50, 65, 80]
    assert ivanov["forecast"]["next"] >= 78 and ivanov["forecast"]["p_pass"] > sokolova["forecast"]["p_pass"]
    assert ivanov["history"][0]["predicted"] is None and ivanov["history"][2]["predicted"] is not None
    assert a["backtest"]["n"] == 4 and "mae" in a["backtest"] and "mae_naive" in a["backtest"]
    assert sokolova["heat"]["ДДС"]["per10"]["1"] == 1.0 and ivanov["heat"]["ДДС"]["counts"]["1"] == 3
    texts = " ".join(i["text"] for i in a["insights"])
    assert "Типичная ошибка (ДДС): «Отсутствует статус реагирования» — у 2 из 2 обучающихся" in texts
    assert "Соколова" in texts and "«информацыю» (3)" in texts
    rec = a["recommendations"][0]
    assert rec["category"] == 1 and "30 секунд" in rec["text"] and rec["preset"] == {"mode": "card_actions", "difficulty": 3}
    assert a["elapsed_ms"] < 30000                                                    # ТЗ: отчёт не дольше 30 с
    assert client.get("/api/analytics?group_id=%d" % g["id"], headers=t2h).status_code == 404
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(client.get("/api/analytics.xlsx?group_id=%d" % g["id"], headers=th).content))
    assert wb.sheetnames == ["Успеваемость", "Прогноз и факт", "Ошибки на 10 карточек"]
    mine = client.get("/api/me/progress", headers=sh).json()
    assert mine["student"]["name"] == "Иванов" and len(mine["student"]["history"]) == 3
    assert mine["student"]["forecast"]["next"] >= 78


# ---------- материалы и рекомендации ----------
def upload(client, headers, name, data, title="", group=""):
    from urllib.parse import quote
    return client.post("/api/materials", content=data, headers=dict(headers, **{
        "X-Filename": quote(name), "X-Title": quote(title), "X-Group": str(group), "X-Description": quote("для занятия")}))


def test_materials_follow_group_visibility(app_env):
    client, engine, clock, db, ids = app_env
    th, _, _ = login(client, "t1")
    t2h, _, _ = login(client, "t2")
    s1h, _, _ = login(client, "s1")
    s2h, _, _ = login(client, "s2")
    g = group_with(client, th, "Материалы", [ids["s1"]])
    pdf = upload(client, th, "Памятка ДДС.pdf", b"%PDF-1.4 test", "Памятка", g["id"]).json()
    common = upload(client, th, "общий.txt", "для всех".encode("utf-8")).json()
    assert pdf["title"] == "Памятка" and pdf["size"] == 13 and pdf["content_type"] == "application/pdf"
    assert upload(client, th, "вирус.exe", b"MZ").status_code == 400
    assert upload(client, th, "пусто.pdf", b"").status_code == 400
    assert upload(client, s1h, "x.pdf", b"%PDF").status_code == 403
    assert {m["id"] for m in client.get("/api/materials", headers=s1h).json()} == {pdf["id"], common["id"]}
    assert {m["id"] for m in client.get("/api/materials", headers=s2h).json()} == {common["id"]}
    got = client.get("/api/materials/%d/file" % pdf["id"], headers=s1h)
    assert got.content == b"%PDF-1.4 test" and got.headers["content-disposition"].startswith("inline")
    assert client.get("/api/materials/%d/file" % pdf["id"], headers=s2h).status_code == 404
    assert client.delete("/api/materials/%d" % pdf["id"], headers=t2h).status_code == 403
    assert client.delete("/api/materials/%d" % pdf["id"], headers=th).json()["ok"]
    assert client.get("/api/materials/%d/file" % pdf["id"], headers=s1h).status_code == 404


def test_teacher_recommendation_reaches_student(app_env):
    client, engine, clock, db, ids = app_env
    th, _, _ = login(client, "t1")
    sh, _, _ = login(client, "s1")
    assert client.post("/api/students/%d/feedback" % ids["s1"], json={"text": "x"}, headers=th).status_code == 404
    group_with(client, th, "Рекомендации", [ids["s1"]])
    client.post("/api/students/%d/feedback" % ids["s1"], json={"text": "Повторить памятку: чужие УК"}, headers=th)
    mine = client.get("/api/me/feedback", headers=sh).json()
    assert mine[0]["text"] == "Повторить памятку: чужие УК" and mine[0]["teacher"] == "Петрова"


def test_scenario_import_from_file(app_env):
    client, engine, clock, db, ids = app_env
    th, _, _ = login(client, "t1")
    from arm112.paths import data_dir
    raw = json.loads((data_dir() / "scenarios" / "dds_shchukino_v0.json").read_text(encoding="utf-8"))
    sc = client.post("/api/scenarios/import", json=dict(raw, title="Щукино из файла"), headers=th).json()
    assert sc["status"] == "draft" and sc["source"] == "import" and sc["cards"] == len(raw["cards"])
    broken = dict(raw, cards=[dict(raw["cards"][0], code="000")])
    assert client.post("/api/scenarios/import", json=broken, headers=th).status_code == 400
