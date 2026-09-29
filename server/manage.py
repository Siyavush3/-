#!/usr/bin/env python3
"""Обслуживание тренажёра из командной строки.

    python server/manage.py init-db                         создать или обновить схему базы
    python server/manage.py create-user LOGIN --name "Имя" --role admin|teacher|student [--password P]
    python server/manage.py reset-password LOGIN            выдать временный пароль
    python server/manage.py list-users
    python server/manage.py seed-demo                       демо-учётки из data/demo/accounts.json
    python server/manage.py seed-history [--teacher LOGIN]  демо-история занятий для показа аналитики
    python server/manage.py backup                          резервная копия сейчас (var/backups)
    python server/manage.py restore FILE.zip                восстановить из копии — при ОСТАНОВЛЕННОМ сервере
    python server/manage.py integrity [--write]             проверка целостности программы / снять эталон
    python server/manage.py make-cert --host 10.0.0.5       самоподписанный сертификат TLS для класса (var/tls)

База — переменная A112_DB (по умолчанию sqlite:///var/arm112.sqlite).
"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "server"), os.path.join(ROOT, "pycore")]

from app.audit import Audit  # noqa: E402
from app.auth import Accounts  # noqa: E402
from arm112.paths import data_dir  # noqa: E402
from store.db import Database, database_url  # noqa: E402
from store.schema import migrate  # noqa: E402


def seed_demo(accounts: Accounts, audit: Audit) -> int:
    """Создаёт недостающие демо-учётки. Возвращает, сколько создано."""
    with open(data_dir() / "demo" / "accounts.json", encoding="utf-8") as f:
        demo = json.load(f)["accounts"]
    created = 0
    for a in demo:
        if accounts.by_login(a["login"]):
            continue
        user, _ = accounts.create(a["login"], a["name"], a["role"], a["password"], must_change=False)
        audit.log("user.create", None, user.login, {"role": user.role, "source": "seed-demo"})
        created += 1
    return created


def make_cert(directory: Path, hosts, days: int) -> int:
    """Самоподписанный сертификат для локальной сети класса (openssl): сервер — по https://, WebSocket — wss://."""
    import ipaddress
    import shutil
    import subprocess
    openssl = shutil.which("openssl")
    if not openssl:
        print("нет openssl: на Windows он есть в Git for Windows (usr/bin), на Ubuntu — пакет openssl", file=sys.stderr)
        return 1
    directory.mkdir(parents=True, exist_ok=True)
    names = []
    for h in dict.fromkeys(list(hosts) + ["localhost", "127.0.0.1"]):
        try:
            ipaddress.ip_address(h)
            names.append("IP:" + h)
        except ValueError:
            names.append("DNS:" + h)
    crt, key = directory / "arm112.crt", directory / "arm112.key"
    # openssl на Windows не открывает пути с кириллицей — запускаем его в папке сертификата с именами без пути
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", str(days),
                    "-keyout", key.name, "-out", crt.name, "-subj", "/CN=%s/O=ARM-112 trainer" % hosts[0],
                    "-addext", "subjectAltName=" + ",".join(names)], check=True, capture_output=True, cwd=str(directory))
    print("сертификат: %s\nключ: %s\nзапуск: python server/run.py --host 0.0.0.0 --certfile %s --keyfile %s"
          % (crt, key, crt, key))
    print("браузеры предупредят о самоподписанном сертификате: установите %s в доверенные корневые на АРМ" % crt.name)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Обслуживание тренажёра ДДС")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    p = sub.add_parser("create-user")
    p.add_argument("login")
    p.add_argument("--name", required=True)
    p.add_argument("--role", required=True, choices=["admin", "teacher", "student"])
    p.add_argument("--password", help="если не задан — спросит; пустой ввод — временный пароль")
    p.add_argument("--temporary", action="store_true", help="не спрашивать: временный пароль со сменой при первом входе")
    p = sub.add_parser("reset-password")
    p.add_argument("login")
    sub.add_parser("list-users")
    sub.add_parser("seed-demo")
    p = sub.add_parser("seed-history")
    p.add_argument("--teacher", default="teacher")
    sub.add_parser("backup")
    p = sub.add_parser("restore")
    p.add_argument("file")
    p = sub.add_parser("integrity")
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("make-cert")
    p.add_argument("--host", action="append", default=[], help="IP-адрес или имя сервера (можно несколько)")
    p.add_argument("--days", type=int, default=825)
    args = parser.parse_args(argv)
    var = Path(os.environ.get("A112_VAR") or os.path.join(ROOT, "var"))
    if args.cmd == "restore":  # до открытия базы: файл базы будет заменён
        from ops.backup import BackupError, restore
        try:
            for line in restore(args.file, database_url(), os.environ.get("A112_MATERIALS_DIR") or var / "materials", ROOT):
                print(line)
        except (BackupError, OSError, KeyError) as exc:
            print("восстановление не выполнено: %s" % exc, file=sys.stderr)
            return 1
        print("готово — запустите сервер")
        return 0
    if args.cmd == "integrity":
        from ops import integrity
        baseline = var / "integrity.json"
        if args.write:
            res = integrity.snapshot(ROOT, baseline)
            print("эталон целостности снят: файлов %d → %s" % (res["files"], baseline))
            return 0
        res = integrity.check(ROOT, baseline)
        if res["ok"] is None:
            print(res["message"])
            return 1
        print("целостность: %s; изменено %d, пропало %d, добавлено %d" % ("в порядке" if res["ok"] else "НАРУШЕНА",
              len(res["changed"]), len(res["missing"]), len(res["added"])))
        for kind in ("changed", "missing", "added"):
            for f in res[kind][:20]:
                print("  %s: %s" % ({"changed": "изменён", "missing": "пропал", "added": "лишний"}[kind], f))
        return 0 if res["ok"] else 2
    if args.cmd == "make-cert":
        return make_cert(var / "tls", args.host or ["localhost"], args.days)

    db = Database()
    version = migrate(db)
    accounts, audit = Accounts(db), Audit(db)
    if args.cmd == "init-db":
        print("схема базы: версия %d (%s)" % (version, db.url))
    elif args.cmd == "create-user":
        # пароль: ключ, переменная A112_PASSWORD (так передаёт установщик — не видно в списке процессов) или вопрос
        password = args.password or os.environ.get("A112_PASSWORD") or None
        if password is None and not args.temporary and sys.stdin.isatty():
            password = getpass.getpass("Пароль (пусто — временный): ") or None
        user, used = accounts.create(args.login, args.name, args.role, password, must_change=password is None)
        audit.log("user.create", None, user.login, {"role": user.role, "source": "manage.py"})
        print("создана учётная запись %s (%s)" % (user.login, user.role))
        if password is None:
            print("временный пароль: %s — сменить при первом входе" % used)
    elif args.cmd == "reset-password":
        user = accounts.by_login(args.login)
        if user is None:
            print("нет учётной записи %s" % args.login, file=sys.stderr)
            return 1
        password = accounts.reset_password(user.id)
        audit.log("user.reset_password", None, user.login, {"source": "manage.py"})
        print("временный пароль для %s: %s" % (user.login, password))
    elif args.cmd == "list-users":
        for u in accounts.list():
            print("%-12s %-8s %s%s" % (u["login"], u["role"], u["name"], "  [заблокирован]" if u["blocked"] else ""))
    elif args.cmd == "seed-demo":
        print("создано демо-учёток: %d (пароли — data/demo/accounts.json)" % seed_demo(accounts, audit))
    elif args.cmd == "backup":
        from ops.backup import BackupError, Backups
        import arm112
        try:
            item = Backups(db, os.environ.get("A112_BACKUP_DIR") or var / "backups", os.environ.get("A112_MATERIALS_DIR")
                           or var / "materials", arm112.__version__).create("вручную (manage.py)")
        except BackupError as exc:
            print("копия не создана: %s" % exc, file=sys.stderr)
            return 1
        audit.log("backup.create", None, item["name"], {"source": "manage.py", "size": item["size"]})
        print("резервная копия: %s (%.1f МБ)" % (item["name"], item["size"] / 2 ** 20))
    elif args.cmd == "seed-history":
        from app.demo_history import seed_history
        teacher = accounts.by_login(args.teacher)
        if teacher is None or teacher.role != "teacher":
            print("нет преподавателя %s — сначала seed-demo" % args.teacher, file=sys.stderr)
            return 1
        with open(data_dir() / "evaluation" / "rubric.json", encoding="utf-8") as f:
            dds = {int(k): v for k, v in json.load(f)["categories"].items()}
        with open(data_dir() / "evaluation" / "rubric_112.json", encoding="utf-8") as f:
            c112 = json.load(f)["categories"]
        students = [{"id": u["id"], "name": u["name"]} for u in accounts.list("student") if u["login"].startswith("student")]
        n = seed_history(db, teacher.id, students, dds, c112)
        audit.log("demo.seed_history", None, args.teacher, {"lessons": n, "students": len(students)})
        print("демо-история: занятий %d, обучающихся %d%s" % (n, len(students), "" if n else " (уже была)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
