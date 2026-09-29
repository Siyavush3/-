"""Занятия, события занятий, результаты — то, что переживает перезапуск сервера."""
from __future__ import annotations

import time
from typing import Callable, List, Optional

from store.db import Database, dumps, loads

INTERRUPTED = "прервано"


def pid_to_id(pid: str) -> Optional[int]:
    """«u12» → 12; остальное (боты, служебные адресаты) — None."""
    return int(pid[1:]) if pid and pid[0] == "u" and pid[1:].isdigit() else None


class Lessons:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    def create(self, teacher_id: int, mode: str, scenario: str, title: str, config: dict,
               state: str, group_id: Optional[int] = None) -> int:
        return self.db.insert("INSERT INTO lessons (teacher_id, mode, scenario, title, group_id, config, state, created_at)"
                              " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (teacher_id, mode, scenario, title, group_id, dumps(config), state, self.clock()))

    def set_state(self, lesson_id: int, state: str, started_at: Optional[float] = None,
                  finished_at: Optional[float] = None) -> None:
        self.db.execute("UPDATE lessons SET state = ?, started_at = COALESCE(?, started_at),"
                        " finished_at = COALESCE(?, finished_at) WHERE id = ?", (state, started_at, finished_at, lesson_id))

    def mark_interrupted(self, running_states) -> int:
        """После перезапуска сервера незавершённые занятия восстановить нельзя — помечаем честно."""
        marks = ",".join("?" for _ in running_states)
        cur = self.db.execute("UPDATE lessons SET state = ?, finished_at = COALESCE(finished_at, ?)"
                              " WHERE state IN (%s)" % marks, (INTERRUPTED, self.clock(), *running_states))
        return cur.rowcount or 0

    def get(self, lesson_id: int) -> Optional[dict]:
        row = self.db.one("SELECT * FROM lessons WHERE id = ?", (lesson_id,))
        if row:
            row["config"] = loads(row["config"], {})
        return row

    def for_teacher(self, teacher_id: int, limit: int = 200) -> List[dict]:
        rows = self.db.query("SELECT * FROM lessons WHERE teacher_id = ? ORDER BY id DESC LIMIT ?", (teacher_id, limit))
        for r in rows:
            r["config"] = loads(r["config"], {})
        return rows


class Events:
    """Журнал событий занятия (evlog): всё, что происходило, с серверным временем."""

    def __init__(self, db: Database):
        self.db = db

    def add(self, lesson_id: int, at: float, kind: str, user_id: Optional[int], data: dict) -> None:
        self.db.execute("INSERT INTO lesson_events (lesson_id, at, kind, user_id, data) VALUES (?, ?, ?, ?, ?)",
                        (lesson_id, at, kind, user_id, dumps(data)))

    def for_lesson(self, lesson_id: int, user_id: Optional[int] = None) -> List[dict]:
        sql = "SELECT at, kind, user_id, data FROM lesson_events WHERE lesson_id = ?"
        params = [lesson_id]
        if user_id is not None:
            sql += " AND (user_id = ? OR user_id IS NULL)"
            params.append(user_id)
        rows = self.db.query(sql + " ORDER BY id", params)
        for r in rows:
            r["data"] = loads(r["data"], {})
        return rows


class Results:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    def save(self, lesson_id: int, participants: List[dict]) -> None:
        with self.db.transaction():
            for p in participants:
                uid = pid_to_id(p["participant"])
                if uid is None:
                    continue
                summary = {k: p[k] for k in ("name", "cards", "by_category", "categories", "timing", "grammar",
                                             "criteria", "passed", "reasons", "cards_detail") if k in p}
                self.db.insert("INSERT INTO results (lesson_id, user_id, role, score, findings, summary, created_at)"
                               " VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (lesson_id, uid, p.get("role"), int(p["score"]), dumps(p["findings"]), dumps(summary),
                                self.clock()))

    def _rows(self, where: str, params) -> List[dict]:
        rows = self.db.query(
            "SELECT r.*, l.title AS lesson_title, l.mode AS lesson_mode, l.teacher_id, l.started_at, l.finished_at,"
            " l.config AS lesson_config,"
            " u.name AS user_name, u.login AS user_login"
            " FROM results r JOIN lessons l ON l.id = r.lesson_id JOIN users u ON u.id = r.user_id"
            " WHERE " + where + " ORDER BY r.id DESC", params)
        for r in rows:
            r["findings"] = loads(r["findings"], [])
            r["summary"] = loads(r["summary"], {})
            r["lesson_config"] = loads(r["lesson_config"], {})
        return rows

    def for_user(self, user_id: int) -> List[dict]:
        return self._rows("r.user_id = ?", (user_id,))

    def for_lesson(self, lesson_id: int) -> List[dict]:
        return self._rows("r.lesson_id = ?", (lesson_id,))

    def for_teacher(self, teacher_id: int) -> List[dict]:
        return self._rows("l.teacher_id = ?", (teacher_id,))

    def get(self, result_id: int) -> Optional[dict]:
        rows = self._rows("r.id = ?", (result_id,))
        return rows[0] if rows else None

    def for_students(self, teacher_id: int, user_ids: List[int]) -> List[dict]:
        """Результаты этих обучающихся в занятиях этого преподавателя — основа аналитики группы."""
        if not user_ids:
            return []
        marks = ",".join("?" for _ in user_ids)
        return self._rows("l.teacher_id = ? AND r.user_id IN (%s)" % marks, (teacher_id, *user_ids))

    def set_expert(self, result_id: int, score: Optional[int], comment: str, by_user: int) -> None:
        self.db.execute("UPDATE results SET expert_score = ?, expert_comment = ?, expert_by = ?, expert_at = ? WHERE id = ?",
                        (score, comment, by_user, self.clock(), result_id))


DRAFT, APPROVED, ARCHIVED = "draft", "approved", "archived"


class Scenarios:
    """Сгенерированные сценарии: черновик → утверждён (доступен для занятий) → архив (узел 4)."""

    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    def create(self, body: dict, mode: str, role: Optional[str], difficulty: int, categories: List[str],
               source: str, author_id: int) -> int:
        now = self.clock()
        with self.db.transaction():
            sid = self.db.insert(
                "INSERT INTO scenarios (slug, title, mode, role, difficulty, categories, status, source, body,"
                " author_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("new-%s-%s" % (author_id, now), body["title"], mode, role, difficulty, dumps(categories), DRAFT,
                 source, dumps(body), author_id, now, now))
            slug = "sc-%d" % sid  # id пакета в движке: не зависит от seed и не повторяется
            self.db.execute("UPDATE scenarios SET slug = ?, body = ? WHERE id = ?", (slug, dumps(dict(body, id=slug)), sid))
        return sid

    def _parse(self, row: Optional[dict]) -> Optional[dict]:
        if row:
            row["body"] = loads(row["body"], {})
            row["categories"] = loads(row["categories"], [])
        return row

    def get(self, sid: int) -> Optional[dict]:
        return self._parse(self.db.one("SELECT s.*, u.name AS author FROM scenarios s LEFT JOIN users u"
                                       " ON u.id = s.author_id WHERE s.id = ?", (sid,)))

    def list(self, statuses=None) -> List[dict]:
        sql = "SELECT s.*, u.name AS author FROM scenarios s LEFT JOIN users u ON u.id = s.author_id"
        params: list = []
        if statuses:
            sql += " WHERE s.status IN (%s)" % ",".join("?" for _ in statuses)
            params = list(statuses)
        return [self._parse(r) for r in self.db.query(sql + " ORDER BY s.id DESC", params)]

    def update(self, sid: int, body: dict, status: Optional[str] = None) -> None:
        self.db.execute("UPDATE scenarios SET body = ?, title = ?, status = COALESCE(?, status), updated_at = ?"
                        " WHERE id = ?", (dumps(body), body["title"], status, self.clock(), sid))

    def set_status(self, sid: int, status: str) -> None:
        self.db.execute("UPDATE scenarios SET status = ?, updated_at = ? WHERE id = ?", (status, self.clock(), sid))


class StudentCards:
    """Карточки 112, сохранённые обучающимися, — источник карточек для занятий ДДС (ТЗ, сценарий 2)."""

    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    def save(self, lesson_id: int, items: List[dict]) -> int:
        n = 0
        with self.db.transaction():
            for it in items:
                uid = pid_to_id(it.get("participant", ""))
                if uid is None:
                    continue
                self.db.insert("INSERT INTO student_cards (lesson_id, user_id, code, card, created_at) VALUES (?, ?, ?, ?, ?)",
                               (lesson_id, uid, it.get("code"), dumps(it), self.clock()))
                n += 1
        return n

    def recent(self, limit: int = 500) -> List[dict]:
        rows = self.db.query("SELECT * FROM student_cards ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["card"] = loads(r["card"], {})
        return rows


class Groups:
    """Учебные группы преподавателя: состав задаёт сам преподаватель, чужие группы ему не видны."""

    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    def create(self, teacher_id: int, name: str) -> int:
        return self.db.insert("INSERT INTO study_groups (name, teacher_id, created_at) VALUES (?, ?, ?)",
                              (name, teacher_id, self.clock()))

    def get(self, group_id: int) -> Optional[dict]:
        row = self.db.one("SELECT * FROM study_groups WHERE id = ?", (group_id,))
        if row:
            row["members"] = self.members(group_id)
        return row

    def for_teacher(self, teacher_id: int) -> List[dict]:
        rows = self.db.query("SELECT * FROM study_groups WHERE teacher_id = ? AND archived = 0 ORDER BY name",
                             (teacher_id,))
        for r in rows:
            r["members"] = self.members(r["id"])
        return rows

    def rename(self, group_id: int, name: str) -> None:
        self.db.execute("UPDATE study_groups SET name = ? WHERE id = ?", (name, group_id))

    def archive(self, group_id: int) -> None:
        self.db.execute("UPDATE study_groups SET archived = 1 WHERE id = ?", (group_id,))

    def members(self, group_id: int) -> List[dict]:
        return self.db.query("SELECT u.id, u.login, u.name FROM group_members m JOIN users u ON u.id = m.user_id"
                             " WHERE m.group_id = ? ORDER BY u.name", (group_id,))

    def set_members(self, group_id: int, user_ids: List[int]) -> None:
        with self.db.transaction():
            self.db.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
            for uid in dict.fromkeys(user_ids):
                self.db.execute("INSERT INTO group_members (group_id, user_id) VALUES (?, ?)", (group_id, uid))

    def of_student(self, user_id: int) -> List[dict]:
        return self.db.query("SELECT g.id, g.name, g.teacher_id, u.name AS teacher FROM group_members m"
                             " JOIN study_groups g ON g.id = m.group_id JOIN users u ON u.id = g.teacher_id"
                             " WHERE m.user_id = ? AND g.archived = 0 ORDER BY g.name", (user_id,))


class Tasks:
    """Задания: группе назначен пакет сценариев с параметрами занятия; выполняется обучающимся самостоятельно."""

    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    def create(self, teacher_id: int, group_id: int, pack_id: str, title: str, difficulty: Optional[int],
               config: dict, note: str = "", due_at: Optional[float] = None) -> int:
        return self.db.insert("INSERT INTO tasks (teacher_id, group_id, pack_id, title, difficulty, config, note, due_at,"
                              " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (teacher_id, group_id, pack_id, title, difficulty, dumps(config), note, due_at, self.clock()))

    def _parse(self, rows: List[dict]) -> List[dict]:
        for r in rows:
            r["config"] = loads(r["config"], {})
        return rows

    def get(self, task_id: int) -> Optional[dict]:
        rows = self._parse(self.db.query("SELECT t.*, g.name AS group_name FROM tasks t JOIN study_groups g"
                                         " ON g.id = t.group_id WHERE t.id = ?", (task_id,)))
        return rows[0] if rows else None

    def for_teacher(self, teacher_id: int) -> List[dict]:
        return self._parse(self.db.query("SELECT t.*, g.name AS group_name FROM tasks t JOIN study_groups g"
                                         " ON g.id = t.group_id WHERE t.teacher_id = ? AND t.archived = 0"
                                         " ORDER BY t.id DESC", (teacher_id,)))

    def for_student(self, user_id: int) -> List[dict]:
        return self._parse(self.db.query(
            "SELECT t.*, g.name AS group_name, u.name AS teacher FROM tasks t JOIN study_groups g ON g.id = t.group_id"
            " JOIN group_members m ON m.group_id = g.id JOIN users u ON u.id = t.teacher_id"
            " WHERE m.user_id = ? AND t.archived = 0 AND g.archived = 0 ORDER BY t.id DESC", (user_id,)))

    def archive(self, task_id: int) -> None:
        self.db.execute("UPDATE tasks SET archived = 1 WHERE id = ?", (task_id,))


class Materials:
    """Учебные материалы: файлы в var/materials, в базе — описание и кому видно (группа или все)."""

    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    def add(self, title: str, filename: str, path: str, size: int, content_type: str, description: str,
            group_id: Optional[int], uploaded_by: int) -> int:
        return self.db.insert("INSERT INTO materials (title, filename, path, size, content_type, description, group_id,"
                              " uploaded_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (title, filename, path, size, content_type, description, group_id, uploaded_by, self.clock()))

    def get(self, material_id: int) -> Optional[dict]:
        return self.db.one("SELECT m.*, u.name AS author FROM materials m JOIN users u ON u.id = m.uploaded_by"
                           " WHERE m.id = ?", (material_id,))

    def all(self) -> List[dict]:
        return self.db.query("SELECT m.*, u.name AS author, g.name AS group_name FROM materials m"
                             " JOIN users u ON u.id = m.uploaded_by LEFT JOIN study_groups g ON g.id = m.group_id"
                             " ORDER BY m.id DESC")

    def for_student(self, user_id: int) -> List[dict]:
        return self.db.query("SELECT m.*, u.name AS author, g.name AS group_name FROM materials m"
                             " JOIN users u ON u.id = m.uploaded_by LEFT JOIN study_groups g ON g.id = m.group_id"
                             " WHERE m.group_id IS NULL OR m.group_id IN"
                             " (SELECT group_id FROM group_members WHERE user_id = ?) ORDER BY m.id DESC", (user_id,))

    def delete(self, material_id: int) -> None:
        self.db.execute("DELETE FROM materials WHERE id = ?", (material_id,))


class Feedback:
    """Рекомендации преподавателя обучающемуся (не привязаны к одному результату)."""

    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db, self.clock = db, clock

    def add(self, teacher_id: int, student_id: int, text: str) -> int:
        return self.db.insert("INSERT INTO feedback (teacher_id, student_id, text, created_at) VALUES (?, ?, ?, ?)",
                              (teacher_id, student_id, text, self.clock()))

    def for_student(self, student_id: int) -> List[dict]:
        return self.db.query("SELECT f.*, u.name AS teacher FROM feedback f JOIN users u ON u.id = f.teacher_id"
                             " WHERE f.student_id = ? ORDER BY f.id DESC", (student_id,))
