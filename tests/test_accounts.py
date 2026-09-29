"""Учётные записи, сеансы, журнал аудита, хранилище (узел 1)."""
import pytest

from app.audit import Audit
from app.auth import LOCK_S, MAX_FAILED, SESSION_TTL_S, Accounts, AuthError
from app.security import hash_password, policy_error, temporary_password, token_hash, verify_password
from store.db import Database
from store.repos import Events, Lessons, Results
from store.schema import MIGRATIONS, current_version, migrate


class Clock:
    def __init__(self):
        self.now = 1_760_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def db(tmp_path):
    database = Database("sqlite:///" + str(tmp_path / "t.sqlite"))
    migrate(database)
    yield database
    database.close()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def accounts(db, clock):
    return Accounts(db, clock)


# ---------- пароли ----------
def test_password_hash_roundtrip():
    stored = hash_password("Secret-123", iterations=1000)
    assert verify_password("Secret-123", stored)
    assert not verify_password("secret-123", stored)
    assert not verify_password("Secret-123", "мусор")
    assert hash_password("Secret-123", 1000) != stored  # соль разная


def test_password_policy():
    assert policy_error("short1")
    assert policy_error("onlyletters")
    assert policy_error("1234567890")
    assert policy_error("Normal-123") is None
    assert all(policy_error(temporary_password()) is None for _ in range(20))


def test_token_is_stored_hashed(accounts, db):
    accounts.create("t1", "Учитель", "teacher", "Teacher-112", must_change=False)
    token, _ = accounts.authenticate("t1", "Teacher-112")
    stored = db.scalar("SELECT token_hash FROM sessions")
    assert stored == token_hash(token) and token not in stored


# ---------- вход ----------
def test_login_and_session(accounts):
    user, _ = accounts.create("T1", "Учитель  Первый", "teacher", "Teacher-112", must_change=False)
    assert user.login == "t1" and user.name == "Учитель Первый"
    token, logged = accounts.authenticate(" t1 ", "Teacher-112")
    assert logged.id == user.id
    assert accounts.session_user(token).id == user.id
    accounts.logout(token)
    assert accounts.session_user(token) is None


def test_session_expires(accounts, clock):
    accounts.create("s1", "Ученик", "student", "Student-112", must_change=False)
    token, _ = accounts.authenticate("s1", "Student-112")
    clock.now += SESSION_TTL_S + 1
    assert accounts.session_user(token) is None


def test_bruteforce_lockout(accounts, clock):
    accounts.create("s1", "Ученик", "student", "Student-112", must_change=False)
    for _ in range(MAX_FAILED - 1):
        with pytest.raises(AuthError, match="неверный"):
            accounts.authenticate("s1", "wrong-1")
    with pytest.raises(AuthError, match="закрыт"):
        accounts.authenticate("s1", "wrong-1")
    with pytest.raises(AuthError, match="закрыт"):
        accounts.authenticate("s1", "Student-112")      # даже верный пароль — пока идёт блокировка
    clock.now += LOCK_S + 1
    token, _ = accounts.authenticate("s1", "Student-112")
    assert token


def test_unknown_login_same_message(accounts):
    with pytest.raises(AuthError, match="неверный логин или пароль"):
        accounts.authenticate("nobody", "Whatever-1")


def test_block_revokes_sessions(accounts):
    user, _ = accounts.create("s1", "Ученик", "student", "Student-112", must_change=False)
    token, _ = accounts.authenticate("s1", "Student-112")
    accounts.set_blocked(user.id, True)
    assert accounts.session_user(token) is None
    with pytest.raises(AuthError, match="заблокирована"):
        accounts.authenticate("s1", "Student-112")
    accounts.set_blocked(user.id, False)
    assert accounts.authenticate("s1", "Student-112")


def test_temporary_password_must_change(accounts):
    user, temp = accounts.create("s1", "Ученик", "student")
    token, logged = accounts.authenticate("s1", temp)
    assert logged.must_change
    other, _ = accounts.authenticate("s1", temp)
    accounts.change_password(user.id, temp, "Brand-new-7", keep_token=token)
    assert accounts.session_user(token) and not accounts.session_user(token).must_change
    assert accounts.session_user(other) is None           # остальные сеансы отозваны
    with pytest.raises(AuthError):
        accounts.change_password(user.id, "wrong", "Another-8")
    with pytest.raises(ValueError):
        accounts.change_password(user.id, "Brand-new-7", "short")


def test_role_change_revokes_sessions(accounts):
    user, _ = accounts.create("u1", "Пользователь", "student", "Student-112", must_change=False)
    token, _ = accounts.authenticate("u1", "Student-112")
    accounts.update(user.id, role="teacher")
    assert accounts.session_user(token) is None
    assert accounts.get(user.id).role == "teacher"


def test_create_validation(accounts):
    accounts.create("u1", "Пользователь", "student", "Student-112")
    with pytest.raises(ValueError, match="занят"):
        accounts.create("U1", "Другой", "student", "Student-112")
    with pytest.raises(ValueError):
        accounts.create("плохой логин", "Имя", "student", "Student-112")
    with pytest.raises(ValueError):
        accounts.create("u2", "Имя", "superuser", "Student-112")
    with pytest.raises(ValueError):
        accounts.create("u3", "Имя", "student", "weak")


# ---------- аудит ----------
def test_audit_chain_detects_tampering(db, clock):
    audit = Audit(db, clock)
    for i in range(5):
        clock.now += 1
        audit.log("test.action", None, "obj%d" % i, {"i": i, "текст": "проверка"})
    assert audit.verify() == {"ok": True, "checked": 5, "broken_at": None}
    third = db.query("SELECT id FROM audit ORDER BY id")[2]["id"]
    db.execute("UPDATE audit SET details = ? WHERE id = ?", ('{"i":99}', third))
    assert audit.verify() == {"ok": False, "checked": 2, "broken_at": third}


def test_audit_detects_deleted_record(db, clock):
    audit = Audit(db, clock)
    for i in range(4):
        audit.log("a", None, str(i))
    second = db.query("SELECT id FROM audit ORDER BY id")[1]["id"]
    db.execute("DELETE FROM audit WHERE id = ?", (second,))
    assert audit.verify()["ok"] is False


def test_audit_filters(db, clock):
    audit = Audit(db, clock)
    audit.log("login.ok", None, None, {"login": "t1"})
    audit.log("lesson.create", None, "L1", {"login": "t1"})
    assert [r["action"] for r in audit.list(action="login")] == ["login.ok"]
    assert len(audit.list(login="t1")) == 2


# ---------- хранилище ----------
def test_migrations_idempotent(db):
    latest = MIGRATIONS[-1][0]
    assert current_version(db) == latest
    assert migrate(db) == latest


def test_transaction_rollback(db, clock):
    lessons = Lessons(db, clock)
    db.execute("INSERT INTO users (login, name, role, password_hash, created_at) VALUES ('t', 'T', 'teacher', 'x', 0)")
    tid = db.scalar("SELECT id FROM users WHERE login = 't'")
    with pytest.raises(RuntimeError):
        with db.transaction():
            lessons.create(tid, "m", "p", "Т", {}, "набор")
            raise RuntimeError("сбой посреди транзакции")
    assert db.scalar("SELECT COUNT(*) AS n FROM lessons") == 0


def test_results_and_events_roundtrip(db, clock, accounts):
    teacher, _ = accounts.create("t1", "Учитель", "teacher", "Teacher-112")
    student, _ = accounts.create("s1", "Ученик", "student", "Student-112")
    lessons, events, results = Lessons(db, clock), Events(db), Results(db, clock)
    lid = lessons.create(teacher.id, "действия с карточками", "pack", "Занятие", {"time_scale": 1}, "набор")
    events.add(lid, clock(), "status", student.id, {"card": "1", "comment": "передано в УК «ПИК»"})
    results.save(lid, [{"participant": "u%d" % student.id, "role": "Поселение Щукино", "score": 85,
                        "findings": [{"category": 1, "detail": "поздно"}], "name": "Ученик", "cards": 3,
                        "categories": [{"category": 1, "title": "т", "weight": 10}]},
                       {"participant": "bot", "score": 0, "findings": []}])
    rows = results.for_user(student.id)
    assert len(rows) == 1 and rows[0]["score"] == 85 and rows[0]["findings"][0]["detail"] == "поздно"
    assert rows[0]["summary"]["cards"] == 3 and rows[0]["lesson_title"] == "Занятие"
    assert events.for_lesson(lid)[0]["data"]["comment"] == "передано в УК «ПИК»"
    assert lessons.mark_interrupted(["набор", "идёт"]) == 1
    assert lessons.get(lid)["state"] == "прервано"
