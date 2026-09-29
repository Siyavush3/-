"""Администратор (узел 7): настройки с подтверждением и аудитом, сервисы, режим обслуживания, предел занятий,
резервные копии (создание, проверка, очистка, восстановление, расписание), журналы и сбои, статистика,
целостность, пакетное обновление контента, TLS."""
import io
import json
import shutil
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from arm112.paths import data_dir
from ops import backup as bk
from ops import integrity
from ops.settings import Settings, SettingsError
from store.db import Database
from store.schema import migrate
from test_api import PASSWORDS, login, make_db, make_engine


@pytest.fixture
def adm(classifier, tmp_path, monkeypatch):
    from app.main import create_app
    monkeypatch.setenv("A112_VAR", str(tmp_path / "var"))
    monkeypatch.setenv("A112_CONTENT_DIR", str(tmp_path / "scenarios"))
    (tmp_path / "scenarios").mkdir()
    engine, clock, pack = make_engine(classifier)
    db, ids = make_db(tmp_path / "admin.sqlite")
    with TestClient(create_app(engine, db, tick_s=0.01)) as client:
        yield client, engine, db, ids, tmp_path


def confirmed(client):
    h, _, _ = login(client, "admin")
    assert client.post("/api/admin/confirm", json={"password": PASSWORDS["admin"]}, headers=h).status_code == 200
    return h


# ---------- настройки ----------
def test_settings_schema_and_validation(tmp_path):
    db = Database("sqlite:///" + str(tmp_path / "s.sqlite"))
    migrate(db)
    s = Settings(db)
    assert s.get("security.password_min") == 8 and s.get("backup.time") == "03:00"
    seen = []
    s.on_change(lambda st, changed: seen.append(sorted(changed)))
    assert len(seen[0]) == len(s.describe())                                          # подписчик получил всё сразу
    diff = s.update({"security.max_failed": 3, "backup.time": "02:30"})
    assert sorted(k for k, _, _ in diff) == ["backup.time", "security.max_failed"] and seen[-1] == sorted(["backup.time", "security.max_failed"])
    for bad in ({"security.max_failed": 99}, {"backup.time": "25:00"}, {"logging.level": "LOUD"},
                {"backup.enabled": "да"}, {"security.max_failed": 3.5}, {"nope": 1}):
        with pytest.raises(SettingsError):
            s.update(bad)
    assert Settings(db).get("backup.time") == "02:30"                                 # сохранено в базе


def test_settings_need_confirmation_are_audited_and_applied(adm):
    client, engine, db, ids, tmp = adm
    h, _, _ = login(client, "admin")
    assert client.put("/api/admin/settings", json={"changes": {"security.password_min": 10}}, headers=h).status_code == 428
    h = confirmed(client)
    assert client.put("/api/admin/settings", json={"changes": {"security.password_min": 5}}, headers=h).status_code == 400
    res = client.put("/api/admin/settings", json={"changes": {"security.password_min": 12, "security.max_failed": 3,
                                                              "performance.monitor_s": 2.5}}, headers=h).json()
    assert {x["key"]: x["value"] for x in res}["security.password_min"] == 12
    details = json.loads(db.one("SELECT details FROM audit WHERE action = 'settings.update'")["details"])
    assert details["changes"]["security.password_min"] == [8, 12]
    assert engine.monitor_period_s == 2.5
    created = client.post("/api/admin/users", json={"login": "u9", "name": "Проверка", "role": "student"}, headers=h).json()
    assert len(created["temporary_password"]) == 12                                   # временный — по новой политике
    th, token, _ = login(client, "t2")
    r = client.post("/api/password", json={"old": PASSWORDS["teacher"], "new": "Longer1234x"}, headers=th)
    assert r.status_code == 400 and "12" in r.json()["detail"]                        # 11 символов — мало
    for _ in range(3):                                                                # после 3 неудач — блокировка
        client.post("/api/login", json={"login": "s2", "password": "wrong-pass-1"})
    assert "закрыт" in client.post("/api/login", json={"login": "s2", "password": PASSWORDS["student"]}).json()["detail"]


def test_teacher_and_student_have_no_admin_functions(adm):
    client, engine, db, ids, tmp = adm
    for who in ("t1", "s1"):
        h, _, _ = login(client, who)
        for path in ("/api/admin/status", "/api/admin/settings", "/api/admin/backups", "/api/admin/logs"):
            assert client.get(path, headers=h).status_code == 403
    h, _, _ = login(client, "admin")
    assert client.get("/api/analytics", headers=h).status_code == 403                 # и админ не лезет в обучение
    assert client.get("/api/results", headers=h).status_code == 403


# ---------- сервисы ----------
def test_services_maintenance_and_lesson_limit(adm):
    client, engine, db, ids, tmp = adm
    h = confirmed(client)
    services = {s["id"]: s for s in client.get("/api/admin/services", headers=h).json()}
    assert services["web"]["fixed"] and not services["maintenance"]["on"]
    client.post("/api/admin/services/maintenance", json={"action": "start"}, headers=h)
    assert client.post("/api/login", json={"login": "s1", "password": PASSWORDS["student"]}).status_code == 503
    th, _, _ = login(client, "t1")                                                    # преподаватель входит
    assert client.post("/api/lessons", json={"pack_id": "dds-shchukino-v0"}, headers=th).status_code == 503
    client.post("/api/admin/services/maintenance", json={"action": "stop"}, headers=h)
    assert client.post("/api/lessons", json={"pack_id": "dds-shchukino-v0"}, headers=th).status_code == 200
    client.put("/api/admin/settings", json={"changes": {"performance.max_lessons": 1}}, headers=h)
    r = client.post("/api/lessons", json={"pack_id": "dds-shchukino-v0"}, headers=th)
    assert r.status_code == 503 and "предельное" in r.json()["detail"]
    tts = client.post("/api/admin/services/tts", json={"action": "stop"}, headers=h).json()
    assert tts["on"] is False
    actions = [r["action"] for r in db.query("SELECT action FROM audit WHERE action LIKE 'service.%' ORDER BY id")]
    assert actions == ["service.start", "service.stop", "service.stop"]


# ---------- резервные копии ----------
def test_backup_create_verify_prune_and_restore(tmp_path):
    db_path = tmp_path / "live.sqlite"
    db = Database("sqlite:///" + str(db_path))
    migrate(db)
    db.execute("INSERT INTO settings (key, value) VALUES ('probe', '\"до копии\"')")
    mats = tmp_path / "materials"
    mats.mkdir()
    (mats / "doc.pdf").write_bytes(b"%PDF test")
    t = [1_760_000_000.0]
    b = bk.Backups(db, tmp_path / "backups", mats, "test", clock=lambda: t[0])
    item = b.create("вручную", by="admin")
    assert item["files"] == 2 and item["materials"] == 1 and item["dialect"] == "sqlite"
    assert b.verify(item["name"])["ok"] is True
    for _ in range(3):
        t[0] += 61
        b.create("по расписанию")
    assert len(b.list()) == 4 and len(b.prune(2)) == 2 and len(b.list()) == 2
    newest = b.list()[0]["name"]
    broken = tmp_path / "backups" / newest
    raw = bytearray(broken.read_bytes())
    raw[len(raw) // 2] ^= 0xFF                                                         # порча посередине архива
    broken.write_bytes(bytes(raw))
    assert b.verify(newest)["ok"] is False
    with pytest.raises(bk.BackupError):
        b.path("../../etc/passwd")
    good = b.list()[1]["name"]
    db.execute("UPDATE settings SET value = '\"после копии\"' WHERE key = 'probe'")
    db.close()
    (mats / "doc.pdf").unlink()
    report = bk.restore(tmp_path / "backups" / good, "sqlite:///" + str(db_path), mats, tmp_path)
    assert any("сохранена" in x for x in report) and (mats / "doc.pdf").read_bytes() == b"%PDF test"
    again = Database("sqlite:///" + str(db_path))
    assert again.scalar("SELECT value FROM settings WHERE key = 'probe'") == '"до копии"'
    again.close()


def test_backup_schedule_is_daily():
    day = time.mktime((2026, 9, 28, 0, 0, 0, 0, 0, -1))
    assert not bk.due(day + 2 * 3600, "03:00", None)                                   # ещё рано
    assert bk.due(day + 3 * 3600 + 60, "03:00", None)                                  # время пришло
    assert not bk.due(day + 5 * 3600, "03:00", day + 3 * 3600 + 30)                     # сегодня уже была
    assert bk.due(day + 86400 + 3 * 3600 + 5, "03:00", day + 3 * 3600 + 30)             # следующие сутки
    assert bk.next_run(day + 4 * 3600, "03:00") == day + 86400 + 3 * 3600


def test_backup_over_api_and_status(adm):
    client, engine, db, ids, tmp = adm
    h, _, _ = login(client, "admin")
    comp = {c["name"]: c for c in client.get("/api/admin/status", headers=h).json()["components"]}
    assert comp["Резервное копирование"]["status"] == "warn"                          # копий ещё нет
    item = client.post("/api/admin/backups", headers=h).json()
    assert item["name"].startswith("arm112-backup-") and (tmp / "var" / "backups" / item["name"]).is_file()
    assert client.post("/api/admin/backups/%s/verify" % item["name"], headers=h).json()["ok"]
    got = client.get("/api/admin/backups/%s/download" % item["name"], headers=h)
    assert zipfile.ZipFile(io.BytesIO(got.content)).read("manifest.json")
    status = client.get("/api/admin/status", headers=h).json()
    comp = {c["name"]: c for c in status["components"]}
    assert comp["Резервное копирование"]["status"] == "ok" and status["backup"]["count"] == 1
    assert {"Веб-сервер", "База данных", "Маршрутизация", "Диск", "Память", "Журнал аудита",
            "Защита канала (TLS)", "Сбои за час"} <= set(comp)
    assert status["system"]["memory_total_mb"] > 0 and status["db"]["dialect"] == "sqlite"
    assert status["lessons"]["limit"] == 50
    assert client.get("/api/admin/backups/nope.zip/download", headers=h).status_code == 404


# ---------- журналы, сбои, статистика ----------
def test_logs_errors_and_usage_stats(adm):
    client, engine, db, ids, tmp = adm
    h, _, _ = login(client, "admin")
    client.get("/api/admin/status", headers=h)
    client.app.state.syslog.failure("tts", "проверочный сбой", key="x")
    entries = client.get("/api/admin/logs?q=/api/admin/status", headers=h).json()
    assert entries and entries[0]["kind"] == "request" and entries[0]["status"] == 200 and "password" not in json.dumps(entries)
    errors_only = client.get("/api/admin/logs?level=ERROR", headers=h).json()
    assert all(e["level"] in ("ERROR", "CRITICAL") for e in errors_only) and errors_only
    errs = client.get("/api/admin/errors", headers=h).json()
    assert errs[0]["message"] == "проверочный сбой"
    csv = client.get("/api/admin/errors.csv", headers=h).content.decode("utf-8")
    assert csv.startswith("﻿Время;Источник;Сообщение") and "проверочный сбой" in csv
    stats = client.get("/api/admin/stats", headers=h).json()
    today = stats["days"][-1]
    assert today["logins"] >= 1 and stats["totals"]["users"]["student"] == 2
    assert any(r["route"].startswith("GET /api/admin/") for r in stats["requests_by_route"])


# ---------- целостность и TLS ----------
def test_integrity_detects_changes(tmp_path):
    root = tmp_path / "app"
    (root / "server").mkdir(parents=True)
    (root / "server" / "a.py").write_text("x = 1", encoding="utf-8")
    (root / "data" / "scenarios").mkdir(parents=True)
    (root / "data" / "scenarios" / "s.json").write_text("{}", encoding="utf-8")
    base = tmp_path / "integrity.json"
    assert integrity.check(root, base)["ok"] is None                                   # эталона нет
    assert integrity.snapshot(root, base)["files"] == 1                                # контент не входит
    assert integrity.check(root, base)["ok"] is True
    (root / "server" / "a.py").write_text("x = 2", encoding="utf-8")
    (root / "server" / "b.py").write_text("", encoding="utf-8")
    (root / "data" / "scenarios" / "t.json").write_text("{}", encoding="utf-8")
    res = integrity.check(root, base)
    assert res["ok"] is False and res["changed"] == ["server/a.py"] and res["added"] == ["server/b.py"]


def test_tls_status_reads_certificate(adm, monkeypatch):
    client, engine, db, ids, tmp = adm
    if not shutil.which("openssl"):
        pytest.skip("нет openssl")
    import manage
    assert manage.make_cert(tmp / "tls", ["10.0.0.5"], 30) == 0
    monkeypatch.setenv("A112_TLS_CERT", str(tmp / "tls" / "arm112.crt"))
    h, _, _ = login(client, "admin")
    tls = client.get("/api/admin/tls", headers=h).json()
    assert tls["https"] is False and 28 <= tls["cert"]["days_left"] <= 30 and "10.0.0.5" in tls["cert"]["subject"]


# ---------- пакетное обновление контента ----------
def test_content_update_backs_up_first_and_validates(adm):
    client, engine, db, ids, tmp = adm
    raw = json.loads((data_dir() / "scenarios" / "dds_shchukino_v0.json").read_text(encoding="utf-8"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("scenarios/dds_new.json", json.dumps(dict(raw, id="dds-new", title="Новый сценарий"), ensure_ascii=False))
        z.writestr("scenarios/broken.json", json.dumps(dict(raw, id="dds-broken", cards=[dict(raw["cards"][0], code="000")])))
        z.writestr("../evil.py", "print(1)")
    h, _, _ = login(client, "admin")
    assert client.post("/api/admin/content", content=buf.getvalue(), headers=h).status_code == 428
    h = confirmed(client)
    rep = client.post("/api/admin/content", content=buf.getvalue(), headers=h).json()
    assert rep["installed"] == ["Новый сценарий"] and len(rep["rejected"]) == 2 and rep["backup"].startswith("arm112-backup-")
    assert (tmp / "scenarios" / "dds_new.json").is_file() and "dds-new" in engine.packs
    assert client.post("/api/admin/content", content=b"not a zip", headers=h).status_code == 400
    th, _, _ = login(client, "t1")
    assert "dds-new" in {p["id"] for p in client.get("/api/packs", headers=th).json()}
