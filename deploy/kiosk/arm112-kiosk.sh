#!/usr/bin/env bash
# Тренажёр ДДС: АРМ обучающегося в режиме киоска (Ubuntu 20.04 и новее).
#
# Открывает АРМ в браузере на весь экран — без адресной строки, вкладок и меню.
# У киоска свой профиль браузера: режим включается, даже если обычный браузер уже открыт,
# и в киоске нет чужих вкладок, расширений и сохранённых данных.
# Пока сервер не отвечает, показывается страница ожидания; она сама перейдёт ко входу.
# Выход из киоска — Alt+F4.
#
#   bash arm112-kiosk.sh [--server URL] [--browser auto|chrome|yandex|chromium|edge|firefox]
#                        [--browser-bin PATH] [--window] [--dry-run]
#                        [--install] [--autostart] [--dir DIR] [-- ДОП. КЛЮЧИ БРАУЗЕРА]
#
#   --window       окно приложения вместо полного экрана (для настройки рабочего места)
#   --install      пункт «Тренажёр ДДС» в меню приложений (киоск при этом не запускается)
#   --autostart    запуск киоска при входе пользователя
#   --dir DIR      каталог для .desktop-файлов вместо стандартных
#   --dry-run      только показать, что будет сделано
#
# Адрес сервера и браузер берутся по порядку: ключи, переменные A112_SERVER и A112_BROWSER,
# файл kiosk.conf рядом со скриптом, значения по умолчанию (http://127.0.0.1:8112, auto).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$HERE/$(basename "${BASH_SOURCE[0]}")"

SERVER="" BROWSER="" BIN="" WINDOW=0 DRYRUN=0 INSTALL=0 AUTOSTART=0 TARGET_DIR=""
EXTRA=()
PERSIST=()   # ключи, которые попадут в ярлык

die() { echo "arm112-kiosk: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --server)      [ $# -ge 2 ] || die "нет значения для $1"; SERVER="$2"; PERSIST+=(--server "$2"); shift 2 ;;
    --browser)     [ $# -ge 2 ] || die "нет значения для $1"; BROWSER="$2"; PERSIST+=(--browser "$2"); shift 2 ;;
    --browser-bin) [ $# -ge 2 ] || die "нет значения для $1"; BIN="$2"; PERSIST+=(--browser-bin "$2"); shift 2 ;;
    --window)      WINDOW=1; PERSIST+=(--window); shift ;;
    --dry-run)     DRYRUN=1; shift ;;
    --install)     INSTALL=1; shift ;;
    --autostart)   AUTOSTART=1; shift ;;
    --dir)         [ $# -ge 2 ] || die "нет значения для $1"; TARGET_DIR="$2"; shift 2 ;;
    -h|--help)     sed -n '2,21p' "$SELF" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --)            shift; EXTRA=("$@"); PERSIST+=(-- "$@"); break ;;
    *)             die "неизвестный ключ: $1 (справка: --help)" ;;
  esac
done

conf() {  # значение KEY из kiosk.conf; последняя строка побеждает
  [ -f "$HERE/kiosk.conf" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$HERE/kiosk.conf" | tail -n 1 | tr -d '\r' | sed 's/[[:space:]]*$//'
}

SERVER="${SERVER:-${A112_SERVER:-$(conf SERVER)}}"
SERVER="${SERVER:-http://127.0.0.1:8112}"
SERVER="${SERVER%/}"
BROWSER="${BROWSER:-${A112_BROWSER:-$(conf BROWSER)}}"
BROWSER="$(printf '%s' "${BROWSER:-auto}" | tr '[:upper:]' '[:lower:]')"

case "$SERVER" in
  http://?*|https://?*) ;;
  *) die "адрес сервера должен начинаться с http:// или https://, а указан: $SERVER" ;;
esac
# Микрофон (вопросы заявителю голосом) браузер даёт только защищённым адресам: HTTPS или localhost.
# Сервер класса по http:// киоск объявляет защищённым — только его, и только в своём профиле.
ORIGIN="$(printf '%s' "$SERVER" | sed -E 's#^(https?://[^/]+).*#\1#')"
HOST="$(printf '%s' "$ORIGIN" | sed -E 's#^https?://##; s#:[0-9]+$##; s#^\[(.*)\]$#\1#')"
INSECURE=0
case "$ORIGIN" in
  http://*) case "$HOST" in localhost|127.*|::1) ;; *) INSECURE=1 ;; esac ;;
esac

# ---------- ярлыки (браузер для них не нужен: его ищет сам киоск при запуске) ----------
desktop_quote() {  # аргумент строки Exec по спецификации Desktop Entry
  local s="$1"
  s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//\`/\\\`}"; s="${s//\$/\\\$}"  # правило кавычек
  s="${s//\\/\\\\}"                       # правило строк поверх него: каждый \ ещё раз удваивается
  s="${s//\%/%%}"                         # % начинает коды полей
  printf '"%s"' "$s"
}

write_desktop() {  # write_desktop ПУТЬ
  local exec_line="bash $(desktop_quote "$SELF")" a
  for a in ${PERSIST[@]+"${PERSIST[@]}"}; do exec_line+=" $(desktop_quote "$a")"; done
  if [ "$DRYRUN" = 1 ]; then echo "desktop: $1"; echo "exec: $exec_line"; return; fi
  mkdir -p "$(dirname "$1")"
  cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Тренажёр ДДС
Comment=АРМ-112 в режиме киоска
Exec=$exec_line
Icon=applications-education
Terminal=false
Categories=Education;
X-GNOME-Autostart-enabled=true
EOF
  echo "Создан ярлык: $1"
}

if [ "$INSTALL" = 1 ] || [ "$AUTOSTART" = 1 ]; then
  if [ "$INSTALL" = 1 ]; then
    write_desktop "${TARGET_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/applications}/arm112-kiosk.desktop"
  fi
  if [ "$AUTOSTART" = 1 ]; then
    write_desktop "${TARGET_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/autostart}/arm112-kiosk-autostart.desktop"
  fi
  exit 0
fi

# ---------- браузер ----------
candidates() {
  case "$1" in
    chrome)   echo google-chrome-stable google-chrome ;;
    yandex)   echo yandex-browser-stable yandex-browser ;;
    chromium) echo chromium chromium-browser ;;
    edge)     echo microsoft-edge-stable microsoft-edge ;;
    firefox)  echo firefox ;;
    *)        return 1 ;;
  esac
}

find_browser() {
  local c
  for c in $(candidates "$1"); do
    if command -v "$c" >/dev/null 2>&1; then command -v "$c"; return 0; fi
  done
  return 1
}

if [ -n "$BIN" ]; then
  [ -x "$BIN" ] || die "не найден исполняемый файл браузера: $BIN"
  if [ "$BROWSER" = auto ]; then
    case "$BIN" in *firefox*) BROWSER=firefox ;; *) BROWSER=chrome ;; esac
  fi
elif [ "$BROWSER" = auto ]; then
  for kind in chrome yandex chromium edge firefox; do
    if BIN="$(find_browser "$kind")"; then BROWSER="$kind"; break; fi
  done
  [ -n "$BIN" ] || die "не найден ни один браузер: Chrome, Яндекс Браузер, Chromium, Edge или Firefox (--browser-bin ПУТЬ)"
else
  candidates "$BROWSER" >/dev/null || die "неизвестный браузер «$BROWSER»: chrome, yandex, chromium, edge, firefox или auto"
  BIN="$(find_browser "$BROWSER")" || die "браузер «$BROWSER» не найден (--browser-bin ПУТЬ)"
fi

# ---------- что открыть ----------
urlencode() {  # процентное кодирование UTF-8 побайтно
  local LC_ALL=C s="$1" out="" c i code
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in
      [a-zA-Z0-9._~/-]) out+="$c" ;;
      *) printf -v code '%d' "'$c"; printf -v c '%%%02X' "$((code & 255))"; out+="$c" ;;
    esac
  done
  printf '%s' "$out"
}

# Страница ожидания лежит на рабочем месте: открывается даже при выключенном сервере.
START_URL="file://$(urlencode "$HERE/waiting.html")?server=$(urlencode "$SERVER")"

# Браузеры из snap не видят скрытые каталоги в домашнем — профиль кладём в их собственный каталог.
case "$BIN" in
  /snap/bin/*) PROFILE="$HOME/snap/$(basename "$BIN")/common/arm112-kiosk" ;;
  *)           PROFILE="${XDG_CACHE_HOME:-$HOME/.cache}/arm112/kiosk-$BROWSER" ;;
esac

if [ "$BROWSER" = firefox ]; then
  ARGS=(--new-instance --profile "$PROFILE")
  [ "$WINDOW" = 1 ] || ARGS+=(--kiosk)
  ARGS+=(${EXTRA[@]+"${EXTRA[@]}"} "$START_URL")
else
  # --autoplay-policy: доклады бригады звучат без предварительного клика по странице;
  # --use-fake-ui-for-media-stream: микрофон оператора — без вопроса о разрешении
  ARGS=("--user-data-dir=$PROFILE" --no-first-run --no-default-browser-check
        --disable-features=Translate --disable-session-crashed-bubble --hide-crash-restore-bubble
        --noerrdialogs --disable-pinch --overscroll-history-navigation=0
        --autoplay-policy=no-user-gesture-required --use-fake-ui-for-media-stream)
  [ "$INSECURE" = 1 ] && ARGS+=("--unsafely-treat-insecure-origin-as-secure=$ORIGIN")
  ARGS+=(${EXTRA[@]+"${EXTRA[@]}"})
  if [ "$WINDOW" = 1 ]; then
    ARGS+=(--start-maximized "--app=$START_URL")
  else
    ARGS+=(--kiosk "$START_URL")
  fi
fi

# ---------- запуск ----------
if [ "$DRYRUN" = 1 ]; then
  echo "browser: $BROWSER"
  echo "exe: $BIN"
  echo "profile: $PROFILE"
  echo "start: $START_URL"
  printf 'command: %q' "$BIN"; printf ' %q' "${ARGS[@]}"; echo
  exit 0
fi

mkdir -p "$PROFILE"
if [ "$BROWSER" = firefox ]; then
  # настройки профиля киоска: без приветствий и восстановления сессии, звук без клика
  cat > "$PROFILE/user.js" <<'EOF'
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.translations.enable", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("media.autoplay.default", 0);
user_pref("media.autoplay.blocking_policy", 0);
user_pref("media.navigator.permission.disabled", true);
EOF
  if [ "$INSECURE" = 1 ]; then
    cat >> "$PROFILE/user.js" <<EOF
user_pref("dom.securecontext.allowlist", "$HOST");
user_pref("media.devices.insecure.enabled", true);
user_pref("media.getusermedia.insecure.enabled", true);
EOF
  fi
fi
setsid "$BIN" "${ARGS[@]}" >/dev/null 2>&1 < /dev/null &
echo "Киоск: $BROWSER, сервер $SERVER"
