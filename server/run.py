#!/usr/bin/env python3
"""Запуск сервера тренажёра.

    python server/run.py                    # только этот компьютер: http://127.0.0.1:8112
    python server/run.py --host 0.0.0.0     # учебный класс: обучающиеся заходят по адресу сервера
    python server/run.py --demo             # при пустой базе создать демо-учётки (data/demo/accounts.json)

База — переменная A112_DB (по умолчанию sqlite:///var/arm112.sqlite). Путь к C-ядру — A112_LIB
(по умолчанию ищется в build/); без ядра маршрутизация идёт через эталон на Python с тем же результатом.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "server"), os.path.join(ROOT, "pycore")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Сервер тренажёра оператора ДДС")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8112)
    parser.add_argument("--demo", action="store_true", help="создать демо-учётки, если в базе нет пользователей")
    parser.add_argument("--certfile", help="сертификат TLS (PEM) — сервер будет доступен по https://")
    parser.add_argument("--keyfile", help="закрытый ключ TLS (PEM)")
    args = parser.parse_args()

    import uvicorn
    from app.audit import Audit
    from app.auth import Accounts
    from manage import seed_demo
    from store.db import Database
    from store.schema import migrate

    db = Database()
    migrate(db)
    accounts = Accounts(db)
    if args.demo and accounts.count() == 0:
        print("созданы демо-учётки: %d (пароли — data/demo/accounts.json)" % seed_demo(accounts, Audit(db)))
    elif accounts.count() == 0:
        print("В базе нет учётных записей. Создайте администратора:\n"
              "  python server/manage.py create-user admin --name \"Администратор\" --role admin")
    db.close()

    if args.certfile:  # мониторинг администратора показывает сертификат и срок его действия
        os.environ["A112_TLS_CERT"] = os.path.abspath(args.certfile)
    from app.main import app
    uvicorn.run(app, host=args.host, port=args.port, ssl_certfile=args.certfile, ssl_keyfile=args.keyfile)


if __name__ == "__main__":
    main()
