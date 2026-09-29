"""Резервное копирование (ТЗ: «не реже 1 раза в сутки»; администратор: проводить копирование и настраивать
его параметры; ограничение: критичные данные не удаляются без процедуры резервного копирования).

Копия — один zip в var/backups: manifest.json (когда, зачем, версия, схема базы, SHA-256 каждого файла),
база и, по настройке, учебные материалы. SQLite копируется онлайн-API sqlite3.backup — сервер не
останавливается; PostgreSQL — через pg_dump, если он установлен, иначе выгрузкой всех таблиц в JSON.
Восстановление — только командой manage.py restore при остановленном сервере: подменять базу под
идущими занятиями нельзя.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable, List, Optional

PREFIX = "arm112-backup-"
TABLES = ["users", "sessions", "audit", "study_groups", "group_members", "scenarios", "tasks", "lessons",
          "lesson_events", "results", "student_cards", "materials", "feedback", "settings", "schema_version"]


class BackupError(Exception):
    pass


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class Backups:
    def __init__(self, db, directory, materials_dir, version: str = "", clock: Callable[[], float] = time.time):
        self.db = db
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.materials_dir = Path(materials_dir)
        self.version = version
        self.clock = clock
        self.last_error: Optional[str] = None

    # ---------- создание ----------
    def _dump_db(self, tmp: Path) -> List[Path]:
        if self.db.dialect == "sqlite":
            target = tmp / "db.sqlite"
            dst = sqlite3.connect(str(target))
            try:
                with self.db.lock:  # онлайн-копия: писатели подождут миллисекунды
                    self.db.conn.backup(dst)
            finally:
                dst.close()
            return [target]
        target = tmp / "db.dump"
        if shutil.which("pg_dump"):
            subprocess.run(["pg_dump", "--format=custom", "--file", str(target), "--dbname", self.db.url],
                           check=True, capture_output=True, timeout=3600)
            return [target]
        target = tmp / "db.json"  # без pg_dump — все таблицы в JSON (восстанавливаются manage.py restore)
        data = {t: self.db.query("SELECT * FROM %s" % t) for t in TABLES}
        target.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
        return [target]

    def create(self, reason: str, include_materials: bool = True, by: str = "") -> dict:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        name = PREFIX + stamp + ".zip"
        n = 2
        while (self.dir / name).exists():
            name = "%s%s-%d.zip" % (PREFIX, stamp, n)
            n += 1
        tmp = Path(tempfile.mkdtemp(prefix="a112-backup-"))
        try:
            files = {p.name: p for p in self._dump_db(tmp)}
            if include_materials and self.materials_dir.is_dir():
                for p in sorted(self.materials_dir.iterdir()):
                    if p.is_file():
                        files["materials/" + p.name] = p
            schema = self.db.scalar("SELECT MAX(version) AS v FROM schema_version")
            manifest = {"format": 1, "created_at": self.clock(), "reason": reason, "by": by, "version": self.version,
                        "schema_version": schema, "dialect": self.db.dialect,
                        "files": {arc: {"size": p.stat().st_size, "sha256": sha256_file(p)} for arc, p in files.items()}}
            part = self.dir / (name + ".part")
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
                for arc, p in files.items():
                    z.write(p, arc)
            os.replace(part, self.dir / name)  # недописанная копия никогда не выглядит готовой
        except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            raise BackupError(self.last_error)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.last_error = None
        return self.info(name)

    # ---------- список, проверка, очистка ----------
    def _manifest(self, path: Path) -> dict:
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read("manifest.json").decode("utf-8"))

    def info(self, name: str) -> dict:
        path = self.path(name)
        m = self._manifest(path)
        return {"name": name, "size": path.stat().st_size, "created_at": m["created_at"], "reason": m.get("reason"),
                "by": m.get("by"), "version": m.get("version"), "schema_version": m.get("schema_version"),
                "dialect": m.get("dialect"), "files": len(m["files"]),
                "materials": sum(1 for f in m["files"] if f.startswith("materials/"))}

    def path(self, name: str) -> Path:
        if "/" in name or "\\" in name or not name.startswith(PREFIX) or not name.endswith(".zip"):
            raise BackupError("нет такой копии")
        path = self.dir / name
        if not path.is_file():
            raise BackupError("нет такой копии")
        return path

    def list(self) -> List[dict]:
        out = []
        for p in sorted(self.dir.glob(PREFIX + "*.zip"), reverse=True):
            try:
                out.append(self.info(p.name))
            except (OSError, KeyError, ValueError, zipfile.BadZipFile):
                out.append({"name": p.name, "size": p.stat().st_size, "created_at": p.stat().st_mtime, "broken": True})
        return sorted(out, key=lambda x: x["created_at"], reverse=True)

    def verify(self, name: str) -> dict:
        """Целостность копии: каждый файл сверяется с SHA-256 из манифеста."""
        path = self.path(name)
        bad = []
        try:
            with zipfile.ZipFile(path) as z:
                m = json.loads(z.read("manifest.json").decode("utf-8"))
                for arc, meta in m["files"].items():
                    h = hashlib.sha256()
                    with z.open(arc) as f:
                        for block in iter(lambda: f.read(1 << 20), b""):
                            h.update(block)
                    if h.hexdigest() != meta["sha256"]:
                        bad.append(arc)
        except (KeyError, zipfile.BadZipFile, ValueError) as exc:
            return {"name": name, "ok": False, "checked": 0, "bad": ["копия повреждена: %s" % exc]}
        return {"name": name, "ok": not bad, "checked": len(m["files"]), "bad": bad}

    def prune(self, keep: int) -> List[str]:
        """Оставить keep самых новых копий; остальные удалить. Возвращает удалённые."""
        items = self.list()
        removed = []
        for item in items[keep:]:
            (self.dir / item["name"]).unlink(missing_ok=True)
            removed.append(item["name"])
        return removed

    def last(self) -> Optional[dict]:
        items = [i for i in self.list() if not i.get("broken")]
        return items[0] if items else None

    def total_size(self) -> int:
        return sum(p.stat().st_size for p in self.dir.glob(PREFIX + "*.zip"))


def due(now: float, at: str, last_scheduled: Optional[float]) -> bool:
    """Пора ли ежедневной копии: наступило время at сегодня, а сегодняшней копии по расписанию ещё нет."""
    lt = time.localtime(now)
    hh, mm = (int(x) for x in at.split(":"))
    today_at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    return now >= today_at and (last_scheduled is None or last_scheduled < today_at)


def next_run(now: float, at: str) -> float:
    lt = time.localtime(now)
    hh, mm = (int(x) for x in at.split(":"))
    today_at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    return today_at if today_at > now else today_at + 86400


def restore(archive, db_url: str, materials_dir, repo_root) -> List[str]:
    """Восстановление из копии (manage.py restore, сервер остановлен). Текущая база сохраняется рядом."""
    archive = Path(archive)
    report = []
    with zipfile.ZipFile(archive) as z:
        m = json.loads(z.read("manifest.json").decode("utf-8"))
        for arc, meta in m["files"].items():
            if hashlib.sha256(z.read(arc)).hexdigest() != meta["sha256"]:
                raise BackupError("копия повреждена: %s" % arc)
        if m["dialect"] == "sqlite":
            if not db_url.startswith("sqlite:"):
                raise BackupError("копия SQLite, а сервер настроен на %s" % db_url.split(":")[0])
            path = Path(db_url.split(":///", 1)[1])
            if not path.is_absolute():
                path = Path(repo_root) / path
            if path.exists():
                keep = path.with_name(path.name + ".before-restore-%s" % time.strftime("%Y%m%d-%H%M%S"))
                shutil.copy2(path, keep)
                report.append("текущая база сохранена: %s" % keep.name)
            for suffix in ("-wal", "-shm"):
                Path(str(path) + suffix).unlink(missing_ok=True)
            path.write_bytes(z.read("db.sqlite"))
            report.append("база восстановлена: %s" % path)
        else:
            raise BackupError("копия PostgreSQL восстанавливается средствами СУБД: pg_restore --clean --dbname <адрес> "
                              "db.dump из архива (см. docs)")
        mats = [a for a in m["files"] if a.startswith("materials/")]
        if mats:
            Path(materials_dir).mkdir(parents=True, exist_ok=True)
            for arc in mats:
                (Path(materials_dir) / arc.split("/", 1)[1]).write_bytes(z.read(arc))
            report.append("учебных материалов восстановлено: %d" % len(mats))
    return report
