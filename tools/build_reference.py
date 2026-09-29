"""Справочные разделы документации — из кода и данных тренажёра, чтобы они не расходились с программой.

    python tools/build_reference.py [--check]

Пишет docs/API.md целиком (HTTP-запросы — из приложения FastAPI: адреса, роли, параметры, тела запросов;
протокол WebSocket) и обновляет блоки между метками <!-- generated:имя --> и <!-- /generated --> в руководствах:
настройки администратора, команды manage.py, критерии оценки, статусы ДДС, схема базы данных.
--check — ничего не менять, только проверить, что документы актуальны (так в тестах).
"""
import argparse
import inspect
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "pycore")]
os.environ.setdefault("A112_TTS", "off")   # приложение создаётся только ради списка маршрутов
os.environ.setdefault("A112_ASR", "off")

ROLE = {"teacher": "преподаватель", "student": "обучающийся", "admin": "администратор"}

# Назначение каждого HTTP-запроса. Новый маршрут без описания здесь — ошибка сборки документации (тест).
SUMMARY = {
    ("GET", "/api/health"): "состояние сервиса: версия, C-ядро, база, проверка текста, озвучка, распознавание речи, идущие занятия",
    ("POST", "/api/login"): "вход по логину и паролю; ответ — токен сеанса и пользователь (с признаком обязательной смены пароля)",
    ("POST", "/api/logout"): "выход: сеанс закрывается",
    ("GET", "/api/me"): "текущий пользователь",
    ("POST", "/api/password"): "смена своего пароля (в том числе временного при первом входе)",
    ("GET", "/api/packs"): "сценарии, доступные для занятий: встроенные и утверждённые",
    ("GET", "/api/lessons"): "занятия: преподавателю — свои, обучающемуся — открытые для него",
    ("POST", "/api/lessons"): "создать занятие по сценарию: темп, нормативы, критерии, группа, источник карточек",
    ("POST", "/api/lessons/{lesson_id}/join"): "войти в занятие (обучающийся)",
    ("POST", "/api/lessons/{lesson_id}/roles"): "назначить обучающемуся роль (службу ДДС или оператора 112)",
    ("POST", "/api/lessons/{lesson_id}/start"): "начать занятие",
    ("POST", "/api/lessons/{lesson_id}/finish"): "завершить занятие: оценка по эталону, результаты сохраняются",
    ("GET", "/api/lessons/{lesson_id}/results"): "итоги занятия по участникам и категориям нарушений",
    ("GET", "/api/results/me"): "свои результаты (кабинет обучающегося)",
    ("GET", "/api/results"): "результаты обучающихся по занятиям преподавателя",
    ("GET", "/api/classifier/call-types"): "типы вызова для поля «Что случилось?» (51 тип, как на стенде)",
    ("GET", "/api/classifier/groups/{group}"): "опросная карта группы классификатора: признаки и вопросы",
    ("GET", "/api/classifier/search"): "поиск типа происшествия по части слов",
    ("GET", "/api/route"): "службы для кода происшествия и признаков — по классификатору (C-ядро)",
    ("GET", "/api/services"): "справочник служб",
    ("POST", "/api/text/check"): "проверка текста: орфография и оформление",
    ("GET", "/api/scenarios/options"): "варианты для генератора сценариев: районы, группы происшествий, сложность",
    ("GET", "/api/scenarios"): "сценарии: черновики, утверждённые, архив",
    ("POST", "/api/scenarios/generate"): "сгенерировать сценарий с эталоном (черновик)",
    ("GET", "/api/scenarios/{sid}"): "сценарий с карточками, эталоном и предпросмотром проверки",
    ("POST", "/api/scenarios/{sid}/cards/{key}/revise"): "поправить карточку по комментарию преподавателя",
    ("POST", "/api/scenarios/{sid}/cards/{key}/exclude"): "убрать карточку из черновика",
    ("POST", "/api/scenarios/{sid}/approve"): "утвердить сценарий — он станет доступен для занятий и заданий",
    ("POST", "/api/scenarios/{sid}/archive"): "убрать сценарий в архив",
    ("POST", "/api/scenarios/import"): "загрузить сценарий из файла JSON (черновиком, с проверкой)",
    ("POST", "/api/scenarios/{sid}/copy"): "копия утверждённого сценария в черновик для правки",
    ("POST", "/api/admin/confirm"): "подтвердить опасные действия паролем администратора (действует 10 минут)",
    ("GET", "/api/admin/users"): "учётные записи",
    ("POST", "/api/admin/users"): "создать учётную запись; ответ — временный пароль",
    ("PATCH", "/api/admin/users/{user_id}"): "изменить имя или роль, заблокировать, разблокировать",
    ("POST", "/api/admin/users/{user_id}/reset-password"): "сбросить пароль; ответ — новый временный пароль",
    ("GET", "/api/admin/audit"): "журнал аудита с отбором по действию, логину и времени",
    ("GET", "/api/admin/audit/verify"): "проверить цепочку хешей журнала аудита",
    ("GET", "/api/students"): "обучающиеся — для состава групп",
    ("GET", "/api/groups"): "свои группы преподавателя с составом",
    ("POST", "/api/groups"): "создать группу",
    ("PATCH", "/api/groups/{gid}"): "переименовать группу или изменить её состав",
    ("DELETE", "/api/groups/{gid}"): "убрать группу в архив",
    ("GET", "/api/me/groups"): "группы обучающегося",
    ("GET", "/api/tasks"): "задания: преподавателю — выданные с выполнением, обучающемуся — назначенные ему",
    ("POST", "/api/tasks"): "назначить задание группе",
    ("DELETE", "/api/tasks/{tid}"): "снять задание",
    ("POST", "/api/tasks/{tid}/start"): "начать самостоятельное выполнение задания (занятие на одного)",
    ("PUT", "/api/results/{rid}/expert"): "экспертная оценка результата с комментарием (фиксируется в журнале аудита)",
    ("GET", "/api/students/{sid}/feedback"): "рекомендации преподавателя обучающемуся",
    ("POST", "/api/students/{sid}/feedback"): "добавить рекомендацию обучающемуся",
    ("GET", "/api/me/feedback"): "рекомендации преподавателей — в кабинете обучающегося",
    ("GET", "/api/lessons/{lesson_id}/report"): "отчёт о занятии: действия по карточкам с секундами, замечания, нормативы",
    ("GET", "/api/lessons/{lesson_id}/report.csv"): "отчёт о занятии в CSV (UTF-8 с BOM, разделитель «;»)",
    ("GET", "/api/lessons/{lesson_id}/report.xlsx"): "отчёт о занятии в Excel",
    ("GET", "/api/analytics"): "аналитика группы: инсайты, тепловые карты, прогноз с проверкой на истории, рекомендации",
    ("GET", "/api/analytics.xlsx"): "аналитика группы в Excel",
    ("GET", "/api/analytics.csv"): "аналитика группы в CSV",
    ("GET", "/api/me/progress"): "свой прогресс: баллы, прогноз, ошибки по категориям, рекомендации",
    ("GET", "/api/materials"): "учебные материалы, доступные пользователю",
    ("POST", "/api/materials"): "загрузить материал (файл — телом запроса, описание — в заголовках X-Title, X-Filename, X-Description, X-Group)",
    ("GET", "/api/materials/{mid}/file"): "скачать материал",
    ("DELETE", "/api/materials/{mid}"): "удалить материал (только загрузивший)",
    ("GET", "/api/admin/status"): "состояние компонентов, нагрузка, занятия, озвучка, распознавание речи",
    ("GET", "/api/admin/history"): "история нагрузки за последний час",
    ("GET", "/api/admin/settings"): "настройки системы с описаниями и пределами",
    ("PUT", "/api/admin/settings"): "изменить настройки (применяются сразу, в журнал аудита — было → стало)",
    ("GET", "/api/admin/services"): "сервисы: веб-сервер, озвучка, распознавание речи, копирование по расписанию, режим обслуживания",
    ("POST", "/api/admin/services/{sid}"): "запустить или остановить сервис",
    ("GET", "/api/admin/backups"): "резервные копии",
    ("POST", "/api/admin/backups"): "сделать резервную копию сейчас",
    ("POST", "/api/admin/backups/{name}/verify"): "проверить копию по манифесту SHA-256",
    ("GET", "/api/admin/backups/{name}/download"): "скачать копию",
    ("GET", "/api/admin/logs"): "системный журнал с отбором по уровню и тексту",
    ("GET", "/api/admin/errors"): "сбои и ошибки текущего запуска",
    ("GET", "/api/admin/errors.csv"): "отчёт об ошибках в CSV",
    ("GET", "/api/admin/stats"): "статистика использования по дням",
    ("POST", "/api/admin/integrity"): "проверить файлы программы по эталону целостности",
    ("GET", "/api/admin/tls"): "состояние защиты канала: HTTPS, сертификат, срок действия",
    ("POST", "/api/admin/content"): "пакетное обновление сценариев: zip с scenarios/*.json (перед установкой — резервная копия)",
}

GROUPS = [
    ("Состояние сервиса", lambda p: p == "/api/health"),
    ("Вход и сеанс", lambda p: p in ("/api/login", "/api/logout", "/api/me", "/api/password")),
    ("Отчёты о занятиях", lambda p: "/report" in p),
    ("Занятия", lambda p: p.startswith("/api/lessons") or p == "/api/packs"),
    ("Результаты и экспертная оценка", lambda p: p.startswith("/api/results")),
    ("Классификатор, службы, проверка текста", lambda p: p.startswith(("/api/classifier", "/api/route", "/api/services", "/api/text"))),
    ("Сценарии", lambda p: p.startswith("/api/scenarios")),
    ("Группы, задания, рекомендации", lambda p: p.startswith(("/api/students", "/api/groups", "/api/me/groups", "/api/tasks",
                                                               "/api/me/feedback"))),
    ("Аналитика и прогресс", lambda p: p.startswith(("/api/analytics", "/api/me/progress"))),
    ("Учебные материалы", lambda p: p.startswith("/api/materials")),
    ("Администрирование", lambda p: p.startswith("/api/admin")),
]

WS = """
## WebSocket

Страницы занятий держат с сервером постоянное соединение WebSocket. Токен сеанса передаётся **первым
сообщением**, а не в адресе (адрес попадает в журналы): `{"t": "hello", "token": "…"}`. Без входа, с чужим
занятием или без прав сервер закрывает соединение кодом 4401, 4403 или 4404. Сообщения — JSON с полем `t` (тип).

### /ws?lesson=L — занятие

Команды, меняющие данные, несут номер `seq`: после обрыва связи страница досылает неподтверждённые команды,
сервер отбрасывает повторы и подтверждает каждую (`status_ack`, `save_ack`). Преподаватель по этому сокету только
получает монитор; управляет занятием он запросами HTTP.

| От страницы | Режим | Назначение |
|---|---|---|
| `hello` | все | вход: `token` сеанса |
| `open_card` | ДДС | открыта карточка (статус «Получена службой» проставляется сам) |
| `set_status` | ДДС | статус карточки: `card`, `status`, `order` (номер наряда), `comment`, `seq` |
| `answer` | 112 | принять входящий вызов |
| `ask` | 112 | вопрос заявителю: `intent` (кнопка) и/или `text`; `via: "voice"` — сказан голосом |
| `say` | 112 | реплика оператора без вопроса (голосом): попадает в разговор, заявитель не отвечает |
| `save_card` | 112 | сохранить карточку 112: `card` (номер, код, признаки, заявитель, адрес, описание, службы), `seq` |

| От сервера | Назначение |
|---|---|
| `state` | снимок занятия при подключении и после восстановления связи: занятие, роль, лента или вызов, итог |
| `card` | новая карточка в ленте ДДС |
| `card_status` | изменение статуса карточки (в том числе системное) |
| `call` | доклад старшего бригады по карточке ДДС: текст и ссылка на звук |
| `status_ack`, `status_rejected` | статус принят или отклонён (недопустимый переход, нет карточки) |
| `incoming` | входящий вызов 112 |
| `caller_say` | реплика заявителя: текст и ссылка на звук |
| `save_ack`, `save_rejected` | карточка 112 сохранена или отклонена |
| `card_result` | разбор сохранённой карточки 112: балл, замечания, эталон |
| `queue_done` | вызовов больше нет |
| `monitor` | монитор преподавателя: участники, карточки, просрочки |
| `results` | итоги занятия — преподавателю |
| `result` | итог занятия — обучающемуся |
| `error` | ошибка команды с пояснением |

### /ws/voice?lesson=L — микрофон оператора 112

После `hello` сервер отвечает `{"t": "ready", "rate": 16000}` или `{"t": "unavailable", "reason": "…"}` (распознавание
выключено, не установлено или все каналы заняты). Дальше страница шлёт двоичные сообщения — звук PCM 16 кГц,
16 бит, моно, кусками по 0,1 с (не больше 2 с в сообщении и не быстрее речи) — и текстовое `{"t": "end"}`, когда
фразу нужно закончить сразу. Сервер отвечает `{"t": "partial", "text": "…"}` по ходу речи и
`{"t": "final", "text": "…", "intent": "address"}` после паузы 0,5 с. Вопрос заявителю страница отправляет в занятие
сообщением `ask` с `via: "voice"`.
"""

DB_TABLES = {
    "users": "учётные записи: логин, имя, роль, хеш пароля (PBKDF2), блокировка, обязательная смена пароля",
    "sessions": "сеансы: хеш токена, срок действия, адрес",
    "audit": "журнал аудита: кто, что, когда, с какого адреса; цепочка хешей prev_hash → hash",
    "study_groups": "учебные группы преподавателей",
    "group_members": "состав групп",
    "scenarios": "сценарии: сгенерированные и загруженные (черновик, утверждён, архив), тело — JSON",
    "lessons": "занятия: режим, сценарий, настройки, состояние, время начала и конца",
    "lesson_events": "журнал событий занятий: каждое действие обучающегося и системы с серверным временем",
    "results": "результаты: балл, замечания по категориям, сводка, экспертная оценка",
    "student_cards": "карточки 112, заполненные обучающимися (источник карточек для занятий ДДС)",
    "materials": "учебные материалы: файл, описание, группа",
    "settings": "настройки администратора (ключ → значение)",
    "tasks": "задания группам: сценарий, параметры, срок",
    "feedback": "рекомендации преподавателя обучающемуся",
    "schema_version": "версия схемы базы (миграции)",
}


# ---------- API ----------
def route_roles(route):
    for d in route.dependant.dependencies:
        call = d.call
        if getattr(call, "__name__", "") == "dep" and call.__closure__:
            for cell in call.__closure__:
                v = cell.cell_contents
                if isinstance(v, tuple) and all(isinstance(x, str) for x in v):
                    return ", ".join(ROLE.get(x, x) for x in v)
        if getattr(call, "__name__", "") == "session":
            return "любой после входа"
    return "без входа"


def type_name(annotation) -> str:
    import types
    import typing
    origin = typing.get_origin(annotation)
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if origin in (typing.Union, types.UnionType) and len(args) == 1:   # Optional[X] — просто X, необязательное
        return type_name(args[0])
    if origin in (list, typing.List):
        return "список"
    if origin in (dict, typing.Dict):
        return "объект"
    s = getattr(annotation, "__name__", None) or str(annotation).replace("typing.", "")
    return {"str": "строка", "int": "целое", "float": "число", "bool": "да/нет", "dict": "объект", "list": "список",
            "Any": "любое"}.get(s, s)


def body_fields(route):
    out = []
    for p in route.dependant.body_params:
        model = p.field_info.annotation
        fields = getattr(model, "model_fields", None)
        if not fields:
            out.append("`%s`" % p.name)
            continue
        for name, f in fields.items():
            req = f.is_required()
            out.append("`%s` (%s%s)" % (name, type_name(f.annotation), "" if req else ", необяз."))
    return out


def api_md(app) -> str:
    from fastapi.routing import APIRoute
    rows = {g: [] for g, _ in GROUPS}
    missing = []
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        for method in sorted(r.methods):
            key = (method, r.path)
            summary = SUMMARY.get(key)
            if summary is None:
                missing.append("%s %s" % key)
                continue
            try:
                stepup = "need_stepup(" in inspect.getsource(r.endpoint)
            except (OSError, TypeError):
                stepup = False
            params = ["`%s`" % q.name for q in r.dependant.query_params]
            details = []
            if params:
                details.append("параметры: " + ", ".join(params))
            body = body_fields(r)
            if body:
                details.append("тело: " + ", ".join(body))
            access = route_roles(r) + (" + пароль (428)" if stepup else "")
            text = summary + ("; " + "; ".join(details) if details else "")
            group = next(g for g, test in GROUPS if test(r.path))
            rows[group].append("| `%s` | `%s` | %s | %s |" % (method, r.path, access, text))
    if missing:
        raise SystemExit("нет описания в SUMMARY (tools/build_reference.py): " + ", ".join(missing))
    parts = ["""# Программный интерфейс

<!-- Файл собирается командой python tools/build_reference.py — правки вручную пропадут. -->

Сервер тренажёра отвечает на запросы HTTP (JSON) и держит соединения WebSocket с открытыми занятиями.
Этим интерфейсом пользуются страницы тренажёра; он же подходит для интеграции и автоматизации (например,
`tools/demo_dds.py`, `tools/loadtest.py`). Интерактивное описание со схемами запросов — `/docs` на работающем
сервере (FastAPI OpenAPI).

## Вход и права

- `POST /api/login` возвращает токен сеанса; дальше — заголовок `Authorization: Bearer <токен>`. Срок сеанса —
  настройка администратора (12 ч по умолчанию), выход — `POST /api/logout`.
- Пока временный пароль не сменён (`POST /api/password`), остальные запросы отвечают 403.
- Колонка «Доступ» — кому разрешён запрос. «+ пароль (428)» — опасное действие администратора: сервер ответит
  428, пока администратор не подтвердит его паролем (`POST /api/admin/confirm`, действует 10 минут).
- Ошибки — код HTTP и `{"detail": "пояснение по-русски"}`: 400 — неверные данные, 401 — нужен вход,
  403 — нет прав, 404 — не найдено, 409 — конфликт состояния (например, занятие уже идёт), 413 — слишком большой
  файл, 428 — нужно подтверждение паролем.

## HTTP
"""]
    for group, _ in GROUPS:
        if rows[group]:
            parts.append("\n### %s\n\n| Метод | Адрес | Доступ | Назначение |\n|---|---|---|---|\n%s\n" % (group, "\n".join(rows[group])))
    parts.append(WS)
    return "".join(parts).rstrip() + "\n"


# ---------- блоки руководств ----------
def settings_md() -> str:
    from ops.settings import SCHEMA
    rows = ["| Раздел | Настройка | Ключ | По умолчанию | Допустимо | Пояснение |", "|---|---|---|---|---|---|"]
    for key, (group, title, kind, default, limits, note) in SCHEMA.items():
        if kind == "bool":
            default = "да" if default else "нет"
        allowed = ("%s–%s" % limits if kind in ("int", "float") and limits else
                   ", ".join(limits) if kind == "choice" and limits else "да / нет" if kind == "bool" else
                   "ЧЧ:ММ" if kind == "time" else "")
        rows.append("| %s | %s | `%s` | %s | %s | %s |" % (group, title, key, default, allowed, note or ""))
    return "\n".join(rows)


def manage_md() -> str:
    text = (ROOT / "server" / "manage.py").read_text(encoding="utf-8")
    doc = text.split('"""')[1]
    rows = ["| Команда | Что делает |", "|---|---|"]
    for line in doc.splitlines():
        m = re.match(r"\s+python server/manage\.py (.+?)\s{2,}(.+)$", line)
        if m:
            rows.append("| `%s` | %s |" % (m.group(1).strip(), m.group(2).strip()))
    return "\n".join(rows)


def rubric_md(name: str) -> str:
    data = json.loads((ROOT / "data" / "evaluation" / name).read_text(encoding="utf-8"))
    rows = ["| № | Нарушение | Штраф, баллов |", "|---|---|---|"]
    for num, c in sorted(data["categories"].items(), key=lambda kv: int(kv[0])):
        rows.append("| %s | %s | %s |" % (num, c["title"], c.get("weight", "—")))
    return "\n".join(rows)


def statuses_md() -> str:
    f = json.loads((ROOT / "data" / "fsm" / "response_status.json").read_text(encoding="utf-8"))
    rows = ["| Статус | Можно поставить следом | Комментарий |", "|---|---|---|",
            "| *(новая карточка)* | %s | — |" % ", ".join(f["initial"]["next"])]
    def comment(status):
        if status in f["comment_required"]:
            return "обязателен: причина и куда передана информация"
        return "—" if status == "Принята" else "по докладу бригады"

    for status, t in f["transitions"].items():
        rows.append("| %s | %s | %s |" % (status, ", ".join(t["next"]) or "—", comment(status)))
    for status in f["terminal"]:
        if status not in f["transitions"]:
            rows.append("| %s | — (завершающий) | %s |" % (status, comment(status)))
    rows.append("")
    rows.append("Системные статусы — %s — проставляются сами (карточка создана, открыта службой). Норматив первичного "
                "статуса — %d с." % (", ".join("«%s»" % s for s in f["system_statuses"]), f["primary_status_norm_s"]))
    return "\n".join(rows)


def db_md() -> str:
    from store.db import Database
    from store.schema import migrate
    db = Database("sqlite:///:memory:")
    migrate(db)
    tables = [r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    rows = ["| Таблица | Назначение | Столбцы |", "|---|---|---|"]
    for t in tables:
        if t.startswith("sqlite_"):
            continue
        cols = [c["name"] for c in db.query("PRAGMA table_info(%s)" % t)]
        rows.append("| `%s` | %s | %s |" % (t, DB_TABLES.get(t, ""), ", ".join(cols)))
    db.close()
    return "\n".join(rows)


BLOCKS = {"settings": settings_md, "manage": manage_md, "rubric-dds": lambda: rubric_md("rubric.json"),
          "rubric-112": lambda: rubric_md("rubric_112.json"), "statuses": statuses_md, "db": db_md}
BLOCK = re.compile(r"(<!-- generated:([\w-]+) -->\n)(.*?)(<!-- /generated -->)", re.S)


def fill_blocks(text: str) -> str:
    def repl(m):
        name = m.group(2)
        if name not in BLOCKS:
            raise SystemExit("неизвестный блок generated:%s" % name)
        return m.group(1) + BLOCKS[name]() + "\n" + m.group(4)
    return BLOCK.sub(repl, text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="только проверить, что документы актуальны")
    a = ap.parse_args()
    from app.main import create_app
    outputs = {ROOT / "docs" / "API.md": api_md(create_app())}
    for p in sorted((ROOT / "docs").glob("*.md")):
        text = p.read_text(encoding="utf-8")
        if "<!-- generated:" in text:
            outputs[p] = fill_blocks(text)
    stale = [p for p, text in outputs.items() if not p.is_file() or p.read_text(encoding="utf-8") != text]
    if a.check:
        for p in stale:
            print("устарел: %s — python tools/build_reference.py" % p.relative_to(ROOT))
        return 1 if stale else 0
    for p in stale:
        p.write_text(outputs[p], encoding="utf-8")
        print("обновлён: %s" % p.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
