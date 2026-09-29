#!/usr/bin/env bash
# Установка сервера тренажёра АРМ-112 на Ubuntu 22.04/24.04 (изолированный контур).
#
#   sudo bash deploy/linux/install.sh [--dir /opt/arm112] [--port 8112] [--db postgresql://…]
#                                     [--tls --server-name 10.0.0.5] [--unattended] [--no-service] [--dry-run]
#
# Запускается из распакованного дистрибутива (tools/make_dist.py --platform linux):
#   1. проверяет python3 ≥ 3.10 и модуль venv (пакеты ОС ставятся заранее: python3-venv, openssl);
#   2. создаёт системного пользователя arm112 и копирует программу в папку установки;
#   3. создаёт .venv и ставит пакеты из папки wheels без интернета;
#   4. создаёт базу, администратора, эталон целостности, при --tls — сертификат;
#   5. регистрирует службу systemd arm112 (перезапуск при сбое) и открывает порт в ufw, если он включён.
set -euo pipefail

DIR=/opt/arm112 PORT=8112 BIND=0.0.0.0 DB="" TLS=0 NAME="" ADMIN=admin UNATTENDED=0 SERVICE=1 DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --bind) BIND="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --tls) TLS=1; shift ;;
    --server-name) NAME="$2"; shift 2 ;;
    --admin) ADMIN="$2"; shift 2 ;;
    --unattended) UNATTENDED=1; shift ;;
    --no-service) SERVICE=0; shift ;;
    --dry-run) DRY=1; shift ;;
    *) echo "неизвестный параметр: $1" >&2; exit 2 ;;
  esac
done
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

step() { printf '== %s\n' "$*"; }
run() {  # run "что делаем" команда…
  local what="$1"; shift
  if [ "$DRY" = 1 ]; then printf '   [проверка] %s\n' "$what"; else printf '   %s\n' "$what"; "$@"; fi
}

step "Проверка"
if [ "$DRY" = 0 ] && [ "$SERVICE" = 1 ] && [ "$(id -u)" != 0 ]; then
  echo "нужны права root (служба systemd): sudo bash $0 …  или --no-service" >&2; exit 1
fi
case "$DIR" in *[![:print:]]*|*[А-Яа-яЁё]*) echo "путь установки — латиницей: $DIR" >&2; exit 1 ;; esac
PY=""
for cand in python3.11 python3.12 python3.10 python3; do
  if command -v "$cand" >/dev/null && "$cand" -c 'import sys, venv; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$(command -v "$cand")"; break
  fi
done
[ -n "$PY" ] || { echo "нужен Python 3.10+ с venv: sudo apt install python3 python3-venv" >&2; exit 1; }
echo "   Python: $PY ($("$PY" -V 2>&1))"

step "Файлы программы → $DIR"
if [ "$SERVICE" = 1 ] && ! id arm112 >/dev/null 2>&1; then
  run "системный пользователь arm112" useradd --system --home-dir "$DIR" --shell /usr/sbin/nologin arm112
fi
if [ "$(readlink -f "$SRC")" != "$(readlink -f "$DIR" 2>/dev/null || echo "$DIR")" ]; then
  run "копирование из $SRC" bash -c "mkdir -p '$DIR' && tar -C '$SRC' --exclude=.venv --exclude=var --exclude=.git --exclude=__pycache__ -cf - . | tar -C '$DIR' -xf -"
fi

step "Окружение Python и пакеты"
run "создание .venv" "$PY" -m venv "$DIR/.venv"
VPY="$DIR/.venv/bin/python"
OFFLINE=()
if [ -d "$DIR/wheels" ]; then OFFLINE=(--no-index --find-links "$DIR/wheels"); else echo "   нет папки wheels — пакеты из интернета"; fi
run "пакеты сервера" "$VPY" -m pip install --disable-pip-version-check "${OFFLINE[@]}" -r "$DIR/requirements/server.txt"
if ls "$DIR"/data/voices/*.onnx >/dev/null 2>&1; then
  run "пакеты озвучки" bash -c "'$VPY' -m pip install --disable-pip-version-check ${OFFLINE[*]} -r '$DIR/requirements/tts.txt' \
      || echo '   озвучка не установлена — реплики будут текстом'"
fi
if ls "$DIR"/data/asr/vosk-model-*.zip >/dev/null 2>&1; then
  run "распознавание речи (Vosk)" bash -c "'$VPY' -m pip install --disable-pip-version-check ${OFFLINE[*]} -r '$DIR/requirements/asr.txt' \
      && '$VPY' '$DIR/tools/fetch_asr.py' --check \
      || echo '   распознавание речи не установлено — вопросы кнопками и текстом'"
fi

step "База данных и учётные записи"
ENVFILE=/etc/arm112.env
[ "$SERVICE" = 1 ] || ENVFILE="$DIR/arm112.env"
run "параметры запуска: $ENVFILE" bash -c "umask 077; { echo 'PYTHONUTF8=1'; echo 'PYTHONIOENCODING=utf-8'; [ -n '$DB' ] && echo 'A112_DB=$DB' || true; } > '$ENVFILE'"
[ -n "$DB" ] && export A112_DB="$DB"
run "схема базы" "$VPY" "$DIR/server/manage.py" init-db
if [ "$DRY" = 0 ] && ! "$VPY" "$DIR/server/manage.py" list-users | grep -q "^$ADMIN "; then
  if [ "$UNATTENDED" = 0 ] && [ -t 0 ]; then
    read -r -s -p "   Пароль администратора «$ADMIN» (Enter — временный, со сменой при первом входе): " PASS; echo
  else PASS=""; fi
  if [ -n "$PASS" ]; then A112_PASSWORD="$PASS" "$VPY" "$DIR/server/manage.py" create-user "$ADMIN" --name "Администратор" --role admin
  else "$VPY" "$DIR/server/manage.py" create-user "$ADMIN" --name "Администратор" --role admin --temporary; fi
  unset PASS
fi

ARGS=(--host "$BIND" --port "$PORT")
if [ "$TLS" = 1 ]; then
  step "Сертификат TLS"
  run "make-cert для ${NAME:-$(hostname -I 2>/dev/null | awk '{print $1}')}" "$VPY" "$DIR/server/manage.py" make-cert --host "${NAME:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
  ARGS+=(--certfile "$DIR/var/tls/arm112.crt" --keyfile "$DIR/var/tls/arm112.key")
fi
run "эталон целостности" "$VPY" "$DIR/server/manage.py" integrity --write

if [ "$SERVICE" = 1 ]; then
  step "Служба systemd arm112"
  UNIT=/etc/systemd/system/arm112.service
  run "права на данные" chown -R arm112:arm112 "$DIR/var" "$DIR/data/scenarios"  # сценарии пишет пакетное обновление
  run "unit: $UNIT" bash -c "sed -e 's#@DIR@#$DIR#g' -e 's#@ARGS@#${ARGS[*]}#g' '$DIR/deploy/linux/arm112.service' > '$UNIT'"
  run "включение и запуск" systemctl daemon-reload
  run "автозапуск" systemctl enable --now arm112
  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    run "ufw: порт $PORT/tcp" ufw allow "$PORT/tcp"
  fi
fi

step "Готово"
SCHEME=http; [ "$TLS" = 1 ] && SCHEME=https
echo "   адрес для класса: $SCHEME://<адрес сервера>:$PORT"
echo "   служба: systemctl status|restart|stop arm112; журнал: journalctl -u arm112 и $DIR/var/logs"
echo "   рабочие места обучающихся: deploy/kiosk (режим киоска)"
