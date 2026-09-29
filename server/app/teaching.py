"""Преподаватель и обучающийся (узел 6): группы, задания, экспертная оценка, отчёты, аналитика,
учебные материалы, рекомендации.

Права (ТЗ): преподаватель работает только со своими группами, заданиями и занятиями; экспертная оценка
меняется только с записью в журнал аудита; обучающийся видит только свои результаты и материалы своих групп.
"""
from __future__ import annotations

import time
import urllib.parse
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from app.auth import User
from domain import analytics, reports
from domain.evaluation import criteria_of, verdict
from domain.lesson import Actor, ActorKind, State

MATERIAL_TYPES = {
    ".pdf": "application/pdf", ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8", ".csv": "text/csv; charset=utf-8",
    ".json": "application/json", ".xml": "application/xml", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".mp4": "video/mp4",
}
INLINE = ("application/pdf", "image/", "audio/", "video/", "text/plain")
MAX_MATERIAL = 50 * 2 ** 20


class GroupIn(BaseModel):
    name: str


class GroupPatch(BaseModel):
    name: Optional[str] = None
    members: Optional[List[int]] = None


class TaskIn(BaseModel):
    group_id: int
    pack_id: str
    title: Optional[str] = None
    note: str = ""
    due_at: Optional[float] = None
    difficulty: Optional[int] = None
    lesson: dict = {}                  # параметры занятия: те же, что при создании занятия преподавателем


class ExpertIn(BaseModel):
    score: Optional[int] = None
    comment: str = ""


class FeedbackIn(BaseModel):
    text: str


def owner_actor(rt) -> Actor:
    """От имени владельца занятия — для самостоятельного задания, которое ведёт сам обучающийся."""
    return Actor(rt.lesson.owner_id, ActorKind.TEACHER)


def install(app, h) -> None:
    st = app.state

    def eng():
        return h.eng()

    def categories() -> dict:
        return {"ДДС": [{"category": int(c), "title": v["title"]} for c, v in sorted(eng().rubric.categories.items())],
                "112": [{"category": int(c), "title": v["title"]}
                        for c, v in sorted(eng().rubric112["categories"].items(), key=lambda x: int(x[0]))]}

    def own_group(user: User, gid: int) -> dict:
        g = st.groups.get(gid)
        if g is None or g["teacher_id"] != user.id or g["archived"]:
            raise HTTPException(404, "нет такой группы")
        return g

    def my_students(user: User) -> List[dict]:
        """Обучающиеся преподавателя: из его групп и из его занятий."""
        seen = {}
        for g in st.groups.for_teacher(user.id):
            for m in g["members"]:
                seen[m["id"]] = {"id": m["id"], "name": m["name"], "login": m["login"]}
        for r in st.results.for_teacher(user.id):
            seen.setdefault(r["user_id"], {"id": r["user_id"], "name": r["user_name"], "login": r["user_login"]})
        return sorted(seen.values(), key=lambda s: s["name"])

    # ---------- обучающиеся и группы ----------
    @app.get("/api/students")
    async def students(user: User = Depends(h.teacher)):
        return [{"id": u["id"], "login": u["login"], "name": u["name"]}
                for u in st.accounts.list("student") if not u["blocked"]]

    @app.get("/api/groups")
    async def groups(user: User = Depends(h.teacher)):
        return st.groups.for_teacher(user.id)

    @app.post("/api/groups")
    async def group_create(body: GroupIn, request: Request, user: User = Depends(h.teacher)):
        name = body.name.strip()
        if not 1 <= len(name) <= 100:
            raise HTTPException(400, "название группы — от 1 до 100 символов")
        gid = st.groups.create(user.id, name)
        h.audit(request, "group.create", user, "group:%d" % gid, name=name)
        return st.groups.get(gid)

    @app.patch("/api/groups/{gid}")
    async def group_update(gid: int, body: GroupPatch, request: Request, user: User = Depends(h.teacher)):
        own_group(user, gid)
        if body.name is not None:
            name = body.name.strip()
            if not 1 <= len(name) <= 100:
                raise HTTPException(400, "название группы — от 1 до 100 символов")
            st.groups.rename(gid, name)
        if body.members is not None:
            known = {u["id"] for u in st.accounts.list("student")}
            unknown = [m for m in body.members if m not in known]
            if unknown:
                raise HTTPException(400, "не обучающиеся: %s" % ", ".join(map(str, unknown)))
            st.groups.set_members(gid, body.members)
        h.audit(request, "group.update", user, "group:%d" % gid, name=body.name,
                members=None if body.members is None else len(body.members))
        return st.groups.get(gid)

    @app.delete("/api/groups/{gid}")
    async def group_archive(gid: int, request: Request, user: User = Depends(h.teacher)):
        own_group(user, gid)
        st.groups.archive(gid)
        h.audit(request, "group.archive", user, "group:%d" % gid)
        return {"ok": True}

    @app.get("/api/me/groups")
    async def my_groups(user: User = Depends(h.student)):
        return st.groups.of_student(user.id)

    # ---------- задания ----------
    def attempts(rows: List[dict], task_id: int) -> List[dict]:
        out = []
        for r in rows:
            if (r.get("lesson_config") or {}).get("task_id") == task_id:
                final = analytics.final_score(r)
                crit = criteria_of((r.get("summary") or {}).get("criteria"))
                out.append({"result_id": r["id"], "lesson": "L%d" % r["lesson_id"], "student_id": r["user_id"],
                            "student": r["user_name"], "score": final, "finished_at": r["finished_at"],
                            **verdict(final, len(r["findings"]), crit)})
        return sorted(out, key=lambda a: a["finished_at"] or 0)

    def task_view(t: dict, rows: List[dict], user: Optional[User] = None) -> dict:
        pack = eng().packs.get(t["pack_id"])
        tries = attempts(rows, t["id"])
        view = {k: t[k] for k in ("id", "title", "group_id", "group_name", "pack_id", "difficulty", "note", "due_at",
                                  "created_at", "config")}
        view.update(teacher=t.get("teacher"), attempts=tries, available=pack is not None,
                    mode="cards" if getattr(pack, "mode", "") == "cards" else "card_actions",
                    role_title=getattr(pack, "role_title", ""), pack_title=getattr(pack, "title", t["pack_id"]),
                    best=max((a["score"] for a in tries), default=None),
                    passed=any(a["passed"] for a in tries))
        if user is not None:  # незавершённое самостоятельное занятие — вернуться в него
            view["active_lesson"] = next((lid for lid, rt in eng().lessons.items()
                                          if rt.config.get("task_id") == t["id"] and user.pid in rt.lesson.seats
                                          and rt.lesson.state in (State.RECRUITING, State.RUNNING)), None)
        return view

    @app.get("/api/tasks")
    async def tasks(user: User = Depends(h.member)):
        if user.is_teacher:
            rows = st.results.for_teacher(user.id)
            return [task_view(t, rows) for t in st.tasks.for_teacher(user.id)]
        rows = st.results.for_user(user.id)
        return [task_view(t, rows, user) for t in st.tasks.for_student(user.id)]

    @app.post("/api/tasks")
    async def task_create(body: TaskIn, request: Request, user: User = Depends(h.teacher)):
        own_group(user, body.group_id)
        params = {k: v for k, v in body.lesson.items() if k in h.LessonIn.model_fields and k not in ("pack_id", "group_id")}
        try:
            lesson = h.LessonIn(pack_id=body.pack_id, **params)
        except ValueError as exc:
            raise HTTPException(400, "параметры задания: %s" % exc)
        pack, _, config = h.lesson_config(lesson)  # та же проверка, что у занятия
        if body.difficulty is not None and body.difficulty not in (1, 2, 3):
            raise HTTPException(400, "сложность — 1, 2 или 3")
        difficulty = body.difficulty or (pack.raw.get("generated") or {}).get("difficulty")
        title = (body.title or pack.title).strip()[:200]
        tid = st.tasks.create(user.id, body.group_id, pack.id, title, difficulty,
                              lesson.model_dump(exclude={"pack_id", "group_id"}), body.note.strip()[:2000], body.due_at)
        h.audit(request, "task.create", user, "task:%d" % tid, group=body.group_id, pack=pack.id)
        return task_view(st.tasks.get(tid), [])

    @app.delete("/api/tasks/{tid}")
    async def task_archive(tid: int, request: Request, user: User = Depends(h.teacher)):
        t = st.tasks.get(tid)
        if t is None or t["teacher_id"] != user.id:
            raise HTTPException(404, "нет такого задания")
        st.tasks.archive(tid)
        h.audit(request, "task.archive", user, "task:%d" % tid)
        return {"ok": True}

    @app.post("/api/tasks/{tid}/start")
    async def task_start(tid: int, request: Request, user: User = Depends(h.student)):
        """Самостоятельное выполнение задания: занятие на одного, владелец — преподаватель задания,
        он видит его в своём списке и мониторе."""
        t = next((x for x in st.tasks.for_student(user.id) if x["id"] == tid), None)
        if t is None:
            raise HTTPException(404, "нет такого задания")
        for lid, rt in eng().lessons.items():
            if rt.config.get("task_id") == tid and user.pid in rt.lesson.seats \
                    and rt.lesson.state in (State.RECRUITING, State.RUNNING):
                return dict(h.lesson_dict(lid), page="op112.html" if rt.cards_mode else "arm.html")
        lesson = h.LessonIn(pack_id=t["pack_id"], **{k: v for k, v in t["config"].items() if k in h.LessonIn.model_fields})
        pack, lesson_pack, config = h.lesson_config(lesson)
        config.update(solo=True, task_id=tid, group_id=t["group_id"])
        actor = Actor("u%d" % t["teacher_id"], ActorKind.TEACHER)
        rt = h.open_lesson(t["teacher_id"], actor, pack, lesson_pack, config)
        lid = rt.lesson.id
        outs = eng().join(lid, user.pid, user.name)
        outs += eng().assign_role(actor, lid, user.pid, "Оператор 112" if rt.cards_mode else lesson_pack.role)
        outs += eng().start(actor, lid)
        st.lessons.set_state(h.lesson_db_id(lid), State.RUNNING.value, started_at=h.clock())
        await h.conns.deliver(outs)
        h.audit(request, "task.start", user, lid, task=tid)
        return dict(h.lesson_dict(lid), page="op112.html" if rt.cards_mode else "arm.html")

    # ---------- экспертная оценка и рекомендации ----------
    @app.put("/api/results/{rid}/expert")
    async def expert(rid: int, body: ExpertIn, request: Request, user: User = Depends(h.teacher)):
        r = st.results.get(rid)
        if r is None or r["teacher_id"] != user.id:
            raise HTTPException(404, "нет такого результата")
        if body.score is not None and not 0 <= body.score <= 100:
            raise HTTPException(400, "экспертная оценка — от 0 до 100 баллов")
        comment = body.comment.strip()[:2000]
        st.results.set_expert(rid, body.score, comment, user.id)
        # ТЗ: изменение оценки — только с фиксацией в журнале аудита (было → стало)
        h.audit(request, "result.expert", user, "result:%d" % rid, lesson="L%d" % r["lesson_id"], student=r["user_login"],
                auto=r["score"], before=r["expert_score"], after=body.score, comment=comment[:200])
        r = st.results.get(rid)
        final = analytics.final_score(r)
        return {"result_id": rid, "score": r["score"], "expert_score": r["expert_score"],
                "expert_comment": r["expert_comment"], "final_score": final,
                **verdict(final, len(r["findings"]), criteria_of(r["summary"].get("criteria")))}

    def sees_student(user: User, sid: int) -> None:
        if sid not in {s["id"] for s in my_students(user)}:
            raise HTTPException(404, "нет такого обучающегося в ваших группах и занятиях")

    @app.get("/api/students/{sid}/feedback")
    async def feedback_list(sid: int, user: User = Depends(h.teacher)):
        sees_student(user, sid)
        return st.feedback.for_student(sid)

    @app.post("/api/students/{sid}/feedback")
    async def feedback_add(sid: int, body: FeedbackIn, request: Request, user: User = Depends(h.teacher)):
        sees_student(user, sid)
        text = body.text.strip()
        if not 1 <= len(text) <= 2000:
            raise HTTPException(400, "рекомендация — от 1 до 2000 символов")
        fid = st.feedback.add(user.id, sid, text)
        h.audit(request, "student.feedback", user, "user:%d" % sid, feedback=fid)
        return st.feedback.for_student(sid)

    @app.get("/api/me/feedback")
    async def my_feedback(user: User = Depends(h.student)):
        return st.feedback.for_student(user.id)

    # ---------- отчёт о занятии ----------
    def lesson_report(user: User, lesson_id: str) -> dict:
        dbid = h.lesson_db_id(lesson_id)
        row = st.lessons.get(dbid)
        if row is None:
            raise HTTPException(404, "нет занятия %s" % lesson_id)
        rows = st.results.for_lesson(dbid)
        if user.is_teacher:
            if row["teacher_id"] != user.id:
                raise HTTPException(403, "чужое занятие")
        else:  # обучающемуся — только своя часть отчёта
            rows = [r for r in rows if r["user_id"] == user.id]
            if not rows:
                raise HTTPException(403, "нет ваших результатов в этом занятии")
        if not rows:
            raise HTTPException(409, "занятие ещё не завершено")
        teacher = st.accounts.get(row["teacher_id"])
        return reports.lesson_report(row, rows, st.events.for_lesson(dbid), teacher.name if teacher else "")

    def download(data: bytes, filename: str, media: str) -> Response:
        return Response(data, media_type=media, headers={"Content-Disposition": 'attachment; filename="%s"' % filename})

    @app.get("/api/lessons/{lesson_id}/report")
    async def report(lesson_id: str, user: User = Depends(h.member)):
        t0 = time.perf_counter()
        rep = lesson_report(user, lesson_id)
        rep["elapsed_ms"] = round(1000 * (time.perf_counter() - t0), 1)
        return rep

    @app.get("/api/lessons/{lesson_id}/report.csv")
    async def report_csv(lesson_id: str, user: User = Depends(h.teacher)):
        rep = lesson_report(user, lesson_id)
        return download(reports.to_csv(reports.summary_table(rep)), "report-%s.csv" % lesson_id, "text/csv; charset=utf-8")

    @app.get("/api/lessons/{lesson_id}/report.xlsx")
    async def report_xlsx(lesson_id: str, user: User = Depends(h.teacher)):
        rep = lesson_report(user, lesson_id)
        data = reports.to_xlsx({"Итоги": reports.summary_table(rep), "Карточки": reports.cards_table(rep)},
                               title="Отчёт о занятии %s" % lesson_id)
        return download(data, "report-%s.xlsx" % lesson_id,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    # ---------- аналитика ----------
    def analysis_for(user: User, group_id: Optional[int], threshold: int) -> dict:
        if not 0 <= threshold <= 100:
            raise HTTPException(400, "порог — от 0 до 100")
        people = own_group(user, group_id)["members"] if group_id else my_students(user)
        rows = st.results.for_students(user.id, [p["id"] for p in people])
        t0 = time.perf_counter()
        out = analytics.analyze(rows, people, categories(), threshold)
        out["elapsed_ms"] = round(1000 * (time.perf_counter() - t0), 1)
        return out

    @app.get("/api/analytics")
    async def analytics_view(group_id: Optional[int] = None, threshold: int = 70, user: User = Depends(h.teacher)):
        return analysis_for(user, group_id, threshold)

    @app.get("/api/analytics.xlsx")
    async def analytics_xlsx(group_id: Optional[int] = None, threshold: int = 70, user: User = Depends(h.teacher)):
        data = reports.to_xlsx(reports.progress_tables(analysis_for(user, group_id, threshold)),
                               title="Успеваемость и прогноз")
        return download(data, "progress.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/api/analytics.csv")
    async def analytics_csv(group_id: Optional[int] = None, threshold: int = 70, user: User = Depends(h.teacher)):
        table = reports.progress_tables(analysis_for(user, group_id, threshold))["Успеваемость"]
        return download(reports.to_csv(table), "progress.csv", "text/csv; charset=utf-8")

    @app.get("/api/me/progress")
    async def my_progress(user: User = Depends(h.student)):
        rows = st.results.for_user(user.id)
        threshold = criteria_of(((rows[0].get("summary") or {}).get("criteria")) if rows else None)["pass_score"]
        out = analytics.analyze(rows, [{"id": user.id, "name": user.name}], categories(), threshold)
        return {"student": out["students"][0], "categories": out["categories"], "threshold": threshold}

    # ---------- учебные материалы ----------
    def material_view(m: dict) -> dict:
        return {k: m.get(k) for k in ("id", "title", "filename", "size", "content_type", "description", "group_id",
                                      "group_name", "author", "uploaded_by", "created_at")}

    @app.get("/api/materials")
    async def materials(user: User = Depends(h.member)):
        rows = st.materials.all() if user.is_teacher else st.materials.for_student(user.id)
        return [material_view(m) for m in rows]

    @app.post("/api/materials")
    async def material_upload(request: Request, user: User = Depends(h.teacher)):
        """Файл — телом запроса, описание — в заголовках (X-Title, X-Filename, X-Description, X-Group,
        значения в URL-кодировке): так не нужен пакет для multipart-форм."""
        q = urllib.parse.unquote
        filename = Path(q(request.headers.get("x-filename", ""))).name.strip()
        title = q(request.headers.get("x-title", "")).strip() or filename
        ext = Path(filename).suffix.lower()
        if ext not in MATERIAL_TYPES:
            raise HTTPException(400, "формат %s не поддерживается: %s" % (ext or "без расширения", ", ".join(MATERIAL_TYPES)))
        group = q(request.headers.get("x-group", "")).strip()
        group_id = int(group) if group.isdigit() else None
        if group_id is not None:
            own_group(user, group_id)
        h.materials_dir.mkdir(parents=True, exist_ok=True)
        path = h.materials_dir / (uuid.uuid4().hex + ext)
        size = 0
        with open(path, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_MATERIAL:
                    f.close()
                    path.unlink()
                    raise HTTPException(413, "файл больше %d МБ" % (MAX_MATERIAL // 2 ** 20))
                f.write(chunk)
        if not size:
            path.unlink()
            raise HTTPException(400, "пустой файл")
        mid = st.materials.add(title[:200], filename[:200], path.name, size, MATERIAL_TYPES[ext],
                               q(request.headers.get("x-description", "")).strip()[:2000], group_id, user.id)
        h.audit(request, "material.upload", user, "material:%d" % mid, file=filename, size=size, group=group_id)
        return material_view(st.materials.get(mid))

    def visible_material(user: User, mid: int) -> dict:
        m = st.materials.get(mid)
        if m is None or (not user.is_teacher and mid not in {x["id"] for x in st.materials.for_student(user.id)}):
            raise HTTPException(404, "нет такого материала")
        return m

    @app.get("/api/materials/{mid}/file")
    async def material_file(mid: int, user: User = Depends(h.member)):
        m = visible_material(user, mid)
        path = h.materials_dir / m["path"]
        if not path.is_file():
            raise HTTPException(410, "файл материала не найден на сервере")
        inline = any(m["content_type"].startswith(p) for p in INLINE)
        return FileResponse(path, media_type=m["content_type"], filename=m["filename"],
                            content_disposition_type="inline" if inline else "attachment")

    @app.delete("/api/materials/{mid}")
    async def material_delete(mid: int, request: Request, user: User = Depends(h.teacher)):
        m = visible_material(user, mid)
        if m["uploaded_by"] != user.id:
            raise HTTPException(403, "удалить материал может только тот, кто его загрузил")
        (h.materials_dir / m["path"]).unlink(missing_ok=True)
        st.materials.delete(mid)
        h.audit(request, "material.delete", user, "material:%d" % mid, file=m["filename"])
        return {"ok": True}
