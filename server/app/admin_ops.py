"""Администратор (узел 7): состояние и нагрузка, сервисы, настройки, резервные копии, журналы и сбои,
статистика использования, целостность, пакетное обновление контента, TLS.

Ограничения ТЗ: администратор не вмешивается в учебный процесс (здесь нет ни оценок, ни сценариев,
ни результатов обучающихся); изменения настроек и сервисов — с повторным вводом пароля и в журнале аудита;
перед обновлением контента — резервная копия.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
import ssl
import time
from typing import Dict, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

import arm112
from app.auth import User
from domain.lesson import State
from ops import backup as bk
from ops import content, integrity, monitor, stats
from ops.settings import SettingsError

SERVICES = {"tts": ("Озвучка реплик", "voice.enabled"), "asr": ("Распознавание речи оператора", "voice.asr"),
            "backup": ("Резервное копирование по расписанию", "backup.enabled"),
            "maintenance": ("Режим обслуживания", "service.maintenance")}


class SettingsIn(BaseModel):
    changes: Dict[str, object]


class ServiceIn(BaseModel):
    action: str  # start | stop


def mask_url(url: str) -> str:
    """Адрес базы без пароля."""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url)


def tls_info(request: Optional[Request] = None) -> dict:
    path = os.environ.get("A112_TLS_CERT")
    info = {"https": bool(request and request.url.scheme == "https"), "cert": None}
    if path and os.path.isfile(path):
        try:
            cert = ssl._ssl._test_decode_cert(path)  # noqa: SLF001 — стандартная библиотека, без сторонних пакетов
            expires = ssl.cert_time_to_seconds(cert["notAfter"])
            subject = ", ".join("%s=%s" % kv for part in cert.get("subject", ()) for kv in part)
            info["cert"] = {"file": os.path.basename(path), "subject": subject, "not_after": expires,
                            "days_left": int((expires - time.time()) // 86400)}
        except (ssl.SSLError, OSError, KeyError, ValueError) as exc:
            info["cert"] = {"file": os.path.basename(path), "error": str(exc)}
    return info


def install(app, h) -> None:
    st = app.state
    cache: Dict[str, object] = {}

    def settings():
        return st.settings

    # ---------- состояние компонентов ----------
    def components(request: Request) -> list:
        out = []
        c = monitor.component
        up = time.time() - st.monitor.started
        out.append(c("Веб-сервер", "ok", "работает %d ч %d мин, версия %s" % (up // 3600, up % 3600 // 60, arm112.__version__)))
        try:
            t0 = time.perf_counter()
            st.db.scalar("SELECT 1 AS x")
            ms = (time.perf_counter() - t0) * 1000
            out.append(c("База данных", "ok" if ms < 50 else "warn", "%s, отклик %.1f мс" % (st.db.dialect, ms)))
        except Exception as exc:  # база недоступна — показать, а не упасть
            out.append(c("База данных", "fail", "нет ответа: %s" % exc))
        eng = h.eng()
        native = "C" in eng.router.engine_name
        out.append(c("Маршрутизация", "ok" if native else "warn",
                     eng.router.engine_name + ("" if native else " — C-ядро не собрано, результат тот же, медленнее")))
        out.append(c("Проверка текста", "ok" if eng.textcheck else "warn",
                     "орфография и оформление" if eng.textcheck else "pymorphy3 не установлен — без орфографии"))
        tts = st.tts.status()
        if not settings().get("voice.enabled"):
            out.append(c("Озвучка", "warn", "остановлена администратором — реплики текстом"))
        elif not tts["available"]:
            out.append(c("Озвучка", "warn", "не установлена (piper или голоса) — реплики текстом"))
        else:
            out.append(c("Озвучка", "warn" if tts["failed"] else "ok", "озвучено %d, в очереди %d, сбоев %d"
                         % (tts["rendered"], tts["pending"], tts["failed"])))
        asr = st.asr.status()
        if not asr["enabled"]:
            out.append(c("Распознавание речи", "warn", "выключено администратором — вопросы кнопками и текстом"))
        elif not asr["installed"]:
            out.append(c("Распознавание речи", "warn", "не установлено (vosk или модель в data/asr) — вопросы кнопками"))
        elif asr["error"]:
            out.append(c("Распознавание речи", "fail", "модель не загрузилась: %s" % asr["error"]))
        else:
            out.append(c("Распознавание речи", "ok", "Vosk, словарь %d слов; микрофонов %d, фраз %d%s"
                         % (asr["words"], asr["streams"], asr["utterances"],
                            ", процессор %.0f%% от длительности речи" % (asr["load"] * 100) if asr["load"] else "")))
        last = st.backups.last()
        if st.backups.last_error:
            out.append(c("Резервное копирование", "fail", "последняя попытка не удалась: %s" % st.backups.last_error))
        elif last is None:
            out.append(c("Резервное копирование", "warn", "копий ещё нет"))
        else:
            age = (time.time() - last["created_at"]) / 3600
            out.append(c("Резервное копирование", "ok" if age < 26 else "warn",
                         "последняя копия %.0f ч назад%s" % (age, "" if age < 26 else " — ТЗ: не реже раза в сутки")))
        s = st.monitor.system()
        free_pct = 100 - s["disk_used_percent"]
        out.append(c("Диск", "fail" if free_pct < 5 or s["disk_free_gb"] < 0.5 else "warn" if free_pct < 10 else "ok",
                     "свободно %.1f ГБ из %.1f" % (s["disk_free_gb"], s["disk_total_gb"])))
        out.append(c("Память", "fail" if s["memory_used_percent"] >= 95 else "warn" if s["memory_used_percent"] >= 85 else "ok",
                     "занято %.0f%% из %d МБ, сервер — %.0f МБ" % (s["memory_used_percent"], s["memory_total_mb"], s["process_rss_mb"])))
        recent = [e for e in st.syslog.errors if e["at"] > time.time() - 3600]
        out.append(c("Сбои за час", "ok" if not recent else "warn" if len(recent) <= 5 else "fail",
                     "нет" if not recent else "%d — см. «Журналы»" % len(recent)))
        if cache.get("audit_at", 0) < time.time() - 300:  # цепочку аудита проверяем не чаще раза в 5 минут
            cache["audit"], cache["audit_at"] = st.audit.verify(), time.time()
        a = cache["audit"]
        out.append(c("Журнал аудита", "ok" if a["ok"] else "fail",
                     "цел, записей %d" % a["checked"] if a["ok"] else "нарушена целостность: запись №%s" % a["broken_at"]))
        tls = tls_info(request)
        out.append(c("Защита канала (TLS)", "ok" if tls["https"] else "warn",
                     "HTTPS" + (", сертификат ещё %d дн." % tls["cert"]["days_left"] if tls["cert"] and "days_left" in tls["cert"] else "")
                     if tls["https"] else "HTTP — в классе запустите с сертификатом (--certfile, --keyfile)"))
        ic = cache.get("integrity")
        out.append(c("Целостность программы", "warn" if not ic or ic["ok"] is None else "ok" if ic["ok"] else "fail",
                     "не проверялась" if not ic else ic.get("message") or ("файлы совпадают с эталоном" if ic["ok"] else
                     "изменено %d, пропало %d, добавлено %d" % (len(ic["changed"]), len(ic["missing"]), len(ic["added"])))))
        return out

    def lessons_now() -> dict:
        eng = h.eng()
        live = [rt for rt in eng.lessons.values() if rt.lesson.state in (State.RECRUITING, State.RUNNING)]
        return {"running": sum(rt.lesson.state is State.RUNNING for rt in live),
                "recruiting": sum(rt.lesson.state is State.RECRUITING for rt in live),
                "participants": sum(len(rt.lesson.seats) for rt in live), "connections": h.conns.count(),
                "limit": settings().get("performance.max_lessons")}

    @app.get("/api/admin/status")
    async def status(request: Request, user: User = Depends(h.admin)):
        comps = components(request)
        last = st.backups.last()
        db_size = os.path.getsize(st.db.path) if st.db.dialect == "sqlite" and os.path.isfile(st.db.path) else None
        return {"health": monitor.health(comps), "components": comps, "system": st.monitor.system(),
                "requests": dict(st.monitor.requests.minute(), total=st.monitor.requests.total, errors_total=st.monitor.requests.errors),
                "lessons": lessons_now(), "tts": st.tts.status(), "asr": st.asr.status(),
                "backup": {"enabled": settings().get("backup.enabled"), "time": settings().get("backup.time"),
                           "last": last, "next_at": bk.next_run(time.time(), settings().get("backup.time")),
                           "error": st.backups.last_error, "count": len(st.backups.list()),
                           "size_mb": round(st.backups.total_size() / 2 ** 20, 1)},
                "db": {"dialect": st.db.dialect, "url": mask_url(st.db.url),
                       "schema_version": st.db.scalar("SELECT MAX(version) AS v FROM schema_version"),
                       "size_mb": round(db_size / 2 ** 20, 2) if db_size is not None else None},
                "logs": {"size_mb": round(st.syslog.size() / 2 ** 20, 2), "level": settings().get("logging.level")},
                "tls": tls_info(request), "version": arm112.__version__, "server_time": time.time()}

    @app.get("/api/admin/history")
    async def history(user: User = Depends(h.admin)):
        return list(st.monitor.history)

    # ---------- настройки и сервисы ----------
    @app.get("/api/admin/settings")
    async def settings_get(user: User = Depends(h.admin)):
        return settings().describe()

    @app.put("/api/admin/settings")
    async def settings_put(body: SettingsIn, request: Request, user: User = Depends(h.admin)):
        h.need_stepup(request)  # ТЗ: базовые настройки безопасности — только с подтверждением прав
        try:
            diff = settings().update(body.changes)
        except SettingsError as exc:
            raise HTTPException(400, str(exc))
        if diff:
            h.audit(request, "settings.update", user, None, changes={k: [a, b] for k, a, b in diff})
        return settings().describe()

    def service_view(sid: str) -> dict:
        title, key = SERVICES[sid]
        on = bool(settings().get(key))
        view = {"id": sid, "title": title, "on": on}
        if sid == "tts":
            s = st.tts.status()
            view["detail"] = ("идёт озвучка, в очереди %d" % s["pending"] if s["running"] else
                              "ожидает новых фраз" if on else "остановлена, в очереди %d" % s["pending"])
        elif sid == "asr":
            s = st.asr.status()
            view["detail"] = ("не установлено — нет vosk или модели" if not s["installed"] else
                              "микрофонов сейчас %d, распознано фраз %d" % (s["streams"], s["utterances"]) if on else
                              "выключено — вопросы только кнопками и текстом")
        elif sid == "backup":
            view["detail"] = "ежедневно в %s, следующая — %s" % (settings().get("backup.time"),
                                                               time.strftime("%d.%m %H:%M", time.localtime(bk.next_run(time.time(), settings().get("backup.time"))))) \
                if on else "выключено — копии только вручную"
        else:
            view["detail"] = "новые занятия и вход обучающихся закрыты" if on else "обычная работа"
        return view

    @app.get("/api/admin/services")
    async def services(user: User = Depends(h.admin)):
        up = time.time() - st.monitor.started
        return [{"id": "web", "title": "Веб-сервер и занятия", "on": True, "fixed": True,
                 "detail": "работает %d ч %d мин; перезапуск — службой ОС (docs)" % (up // 3600, up % 3600 // 60)}] + \
            [service_view(s) for s in SERVICES]

    @app.post("/api/admin/services/{sid}")
    async def service_action(sid: str, body: ServiceIn, request: Request, user: User = Depends(h.admin)):
        if sid not in SERVICES or body.action not in ("start", "stop"):
            raise HTTPException(404, "нет такого сервиса или действия")
        h.need_stepup(request)
        settings().update({SERVICES[sid][1]: body.action == "start"})
        h.audit(request, "service.%s" % body.action, user, sid)
        return service_view(sid)

    # ---------- резервные копии ----------
    async def make_backup(reason: str, by: str) -> dict:
        try:
            item = await asyncio.to_thread(st.backups.create, reason, settings().get("backup.include_materials"), by)
        except bk.BackupError as exc:
            st.syslog.failure("backup", "резервная копия не создана: %s" % exc, reason=reason)
            raise
        st.backups.prune(settings().get("backup.keep"))
        st.syslog.log(logging.INFO, "резервная копия %s" % item["name"], kind="backup", reason=reason, size=item["size"])
        return item

    h.make_backup = make_backup

    @app.get("/api/admin/backups")
    async def backups(user: User = Depends(h.admin)):
        return {"items": st.backups.list(), "enabled": settings().get("backup.enabled"), "time": settings().get("backup.time"),
                "keep": settings().get("backup.keep"), "next_at": bk.next_run(time.time(), settings().get("backup.time")),
                "error": st.backups.last_error, "dir": str(st.backups.dir)}

    @app.post("/api/admin/backups")
    async def backup_now(request: Request, user: User = Depends(h.admin)):
        try:
            item = await make_backup("вручную", user.login)
        except bk.BackupError as exc:
            raise HTTPException(500, "копия не создана: %s" % exc)
        h.audit(request, "backup.create", user, item["name"], size=item["size"])
        return item

    @app.post("/api/admin/backups/{name}/verify")
    async def backup_verify(name: str, request: Request, user: User = Depends(h.admin)):
        try:
            res = await asyncio.to_thread(st.backups.verify, name)
        except bk.BackupError as exc:
            raise HTTPException(404, str(exc))
        h.audit(request, "backup.verify", user, name, ok=res["ok"])
        return res

    @app.get("/api/admin/backups/{name}/download")
    async def backup_download(name: str, request: Request, user: User = Depends(h.admin)):
        try:
            path = st.backups.path(name)
        except bk.BackupError as exc:
            raise HTTPException(404, str(exc))
        h.audit(request, "backup.download", user, name)
        return FileResponse(path, media_type="application/zip", filename=name)

    # ---------- журналы и сбои ----------
    @app.get("/api/admin/logs")
    async def logs(level: str = "", q: str = "", limit: int = 300, user: User = Depends(h.admin)):
        return st.syslog.read(max(1, min(limit, 2000)), level or None, q.strip())

    def failures() -> list:
        out = [dict(e) for e in st.syslog.errors]
        for r in st.db.query("SELECT at, action, object, details FROM audit WHERE action IN "
                             "('system.lessons_interrupted', 'scenario.invalid') ORDER BY id DESC LIMIT 100"):
            out.append({"at": r["at"], "source": "server", "message": {"system.lessons_interrupted": "занятия прерваны перезапуском сервера",
                                                                       "scenario.invalid": "сценарий не прошёл проверку при запуске"}[r["action"]]
                        + (": %s" % r["details"] if r["details"] else ""), "object": r["object"]})
        tts = st.tts.status()
        if tts.get("last_error"):
            out.append({"at": time.time(), "source": "tts", "message": "озвучка: %s" % tts["last_error"]})
        return sorted(out, key=lambda e: e["at"], reverse=True)

    @app.get("/api/admin/errors")
    async def errors(user: User = Depends(h.admin)):
        return failures()

    @app.get("/api/admin/errors.csv")
    async def errors_csv(user: User = Depends(h.admin)):
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        w.writerow(["Время", "Источник", "Сообщение", "Адрес", "Ошибка"])
        for e in failures():
            w.writerow([time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(e["at"])), e.get("source"), e.get("message"),
                        e.get("path", ""), e.get("error", "")])
        return Response(("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="errors.csv"'})

    # ---------- статистика, целостность, TLS ----------
    @app.get("/api/admin/stats")
    async def usage(days: int = 14, user: User = Depends(h.admin)):
        out = stats.usage(st.db, max(1, min(days, 90)))
        out["requests_by_route"] = sorted(({"route": k, "count": v} for k, v in st.monitor.requests.by_route.items()),
                                          key=lambda x: -x["count"])[:15]
        return out

    @app.post("/api/admin/integrity")
    async def integrity_check(request: Request, user: User = Depends(h.admin)):
        res = await asyncio.to_thread(integrity.check, h.repo_root, h.integrity_file)
        cache["integrity"] = res
        h.audit(request, "integrity.check", user, None, ok=res["ok"])
        return res

    @app.get("/api/admin/tls")
    async def tls(request: Request, user: User = Depends(h.admin)):
        return tls_info(request)

    # ---------- пакетное обновление контента ----------
    @app.post("/api/admin/content")
    async def content_update(request: Request, user: User = Depends(h.admin)):
        h.need_stepup(request)
        data = await request.body()
        if len(data) > content.MAX_TOTAL:
            raise HTTPException(413, "пакет больше 200 МБ")
        try:
            checked = content.inspect(data, h.eng().build_pack)
        except content.UpdateError as exc:
            raise HTTPException(400, str(exc))
        if not checked["ok"]:
            return {"installed": [], "replaced": [], "backup": None,
                    "rejected": [{"file": r["file"], "error": r["error"]} for r in checked["rejected"]]}
        try:
            copy = await make_backup("перед обновлением контента", user.login)  # ТЗ: сначала резервная копия
        except bk.BackupError as exc:
            raise HTTPException(500, "обновление отменено: не удалось сделать резервную копию (%s)" % exc)
        report = content.install(checked, h.scenarios_dir, h.eng().packs)
        for item in checked["ok"]:
            h.enqueue_tts(item["raw"])
        report["backup"] = copy["name"]
        h.audit(request, "content.update", user, None, installed=report["installed"], replaced=report["replaced"],
                rejected=len(report["rejected"]), backup=copy["name"])
        return report
