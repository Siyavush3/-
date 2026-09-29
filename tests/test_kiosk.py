"""Киоск на рабочих местах: какой браузер и с какими ключами запускается. Сам браузер не запускаем."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from arm112.paths import REPO_ROOT

KIOSK = REPO_ROOT / "deploy" / "kiosk"
KEYS = ("browser", "exe", "profile", "start", "command")

windows = pytest.mark.skipif(sys.platform != "win32", reason="лаунчер PowerShell — для Windows")
posix = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("bash"), reason="лаунчер bash — для Ubuntu")


def clean_env():
    """Настройки машины (A112_SERVER, A112_BROWSER) не должны влиять на тест."""
    return {k: v for k, v in os.environ.items() if k not in ("A112_SERVER", "A112_BROWSER")}


def parse(out: str) -> dict:
    info = {}
    for line in out.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key in KEYS:
            info[key] = value
    return info


def check_start(url: str, server: str) -> None:
    """Старт — страница ожидания на рабочем месте, адрес сервера передан параметром."""
    u = urlparse(url)
    assert u.scheme == "file" and url.isascii()  # путь закодирован: кириллица в пути к проекту не ломает запуск
    local = unquote(u.path)
    if sys.platform == "win32":
        local = local.lstrip("/")
    assert Path(local).resolve() == (KIOSK / "waiting.html").resolve()
    assert parse_qs(u.query)["server"] == [server]


def run_ps(*args):
    res = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", str(KIOSK / "arm112-kiosk.ps1"), *args],
                         capture_output=True, env=clean_env(), timeout=120)
    return res.returncode, res.stdout.decode("utf-8", "replace"), res.stderr.decode("utf-8", "replace")


def run_sh(*args):
    res = subprocess.run(["bash", str(KIOSK / "arm112-kiosk.sh"), *args],
                         capture_output=True, env=clean_env(), timeout=60)
    return res.returncode, res.stdout.decode("utf-8", "replace"), res.stderr.decode("utf-8", "replace")


@pytest.fixture
def fake_exe(tmp_path):
    def make(name):
        path = tmp_path / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"MZ")
        return str(path)
    return make


@windows
def test_windows_fullscreen_kiosk(fake_exe):
    code, out, err = run_ps("-DryRun", "-BrowserPath", fake_exe("chrome.exe"), "-Server", "http://10.0.0.5:8112/")
    assert code == 0, err
    info = parse(out)
    assert info["browser"] == "chrome"
    check_start(info["start"], "http://10.0.0.5:8112")
    cmd = info["command"]
    assert "--kiosk" in cmd and "--app=" not in cmd
    assert "--user-data-dir=" in cmd and "--autoplay-policy=no-user-gesture-required" in cmd
    # микрофон оператора (узел 9): без вопроса о разрешении; сервер класса по http:// — «защищённый»
    assert "--use-fake-ui-for-media-stream" in cmd
    assert "--unsafely-treat-insecure-origin-as-secure=http://10.0.0.5:8112" in cmd


@windows
def test_windows_local_or_https_server_needs_no_insecure_flag(fake_exe):
    for server in ("http://127.0.0.1:8112", "https://10.0.0.5:8112"):
        cmd = parse(run_ps("-DryRun", "-BrowserPath", fake_exe("chrome.exe"), "-Server", server)[1])["command"]
        assert "--use-fake-ui-for-media-stream" in cmd and "unsafely-treat-insecure-origin" not in cmd, server


@windows
def test_windows_edge_modes(fake_exe):
    edge = fake_exe("msedge.exe")
    info = parse(run_ps("-DryRun", "-Browser", "edge", "-BrowserPath", edge)[1])
    assert "--kiosk" in info["command"] and "--edge-kiosk-type=fullscreen" in info["command"]
    info = parse(run_ps("-DryRun", "-Window", "-Browser", "edge", "-BrowserPath", edge)[1])
    assert "--app=file:///" in info["command"] and "--kiosk" not in info["command"]


@windows
def test_windows_firefox_uses_own_profile(fake_exe):
    info = parse(run_ps("-DryRun", "-BrowserPath", fake_exe("firefox.exe"))[1])
    assert info["browser"] == "firefox"
    assert "--kiosk" in info["command"] and "--profile" in info["command"]
    check_start(info["start"], "http://127.0.0.1:8112")


@windows
def test_windows_install_creates_shortcuts(fake_exe, tmp_path):
    target = tmp_path / "ярлыки"
    code, out, err = run_ps("-Install", "-Autostart", "-ShortcutDir", str(target),
                            "-BrowserPath", fake_exe("chrome.exe"), "-Server", "http://10.0.0.5:8112")
    assert code == 0, err
    assert sorted(p.name for p in target.glob("*.lnk")) == ["Тренажёр ДДС — автозапуск.lnk", "Тренажёр ДДС.lnk"]
    # Аргументы ярлыка хранятся в UTF-16: путь к скрипту (в нём может быть кириллица) не искажён
    data = (target / "Тренажёр ДДС.lnk").read_bytes()
    assert str(KIOSK / "arm112-kiosk.ps1").encode("utf-16-le") in data
    assert "-Server http://10.0.0.5:8112".encode("utf-16-le") in data


@windows
def test_windows_rejects_bad_server(fake_exe):
    code, out, err = run_ps("-DryRun", "-BrowserPath", fake_exe("chrome.exe"), "-Server", "ftp://10.0.0.5")
    assert code != 0


@posix
def test_linux_fullscreen_kiosk():
    code, out, err = run_sh("--dry-run", "--browser", "chrome", "--browser-bin", "/bin/true",
                            "--server", "http://10.0.0.5:8112/")
    assert code == 0, err
    info = parse(out)
    assert info["browser"] == "chrome"
    check_start(info["start"], "http://10.0.0.5:8112")
    assert "--kiosk" in info["command"] and "--user-data-dir=" in info["command"]
    assert "--use-fake-ui-for-media-stream" in info["command"]
    assert "--unsafely-treat-insecure-origin-as-secure=http://10.0.0.5:8112" in info["command"]
    local = parse(run_sh("--dry-run", "--browser", "chrome", "--browser-bin", "/bin/true",
                         "--server", "http://127.0.0.1:8112")[1])["command"]
    assert "unsafely-treat-insecure-origin" not in local


@posix
def test_linux_firefox_window_mode():
    code, out, err = run_sh("--dry-run", "--window", "--browser", "firefox", "--browser-bin", "/bin/true")
    assert code == 0, err
    cmd = parse(out)["command"]
    assert "--profile" in cmd and "--kiosk" not in cmd


@posix
def test_linux_install_writes_desktop_entries(tmp_path):
    target = tmp_path / "ярлыки"
    code, out, err = run_sh("--install", "--autostart", "--dir", str(target), "--server", "http://10.0.0.5:8112")
    assert code == 0, err
    entry = (target / "arm112-kiosk.desktop").read_text(encoding="utf-8")
    assert (target / "arm112-kiosk-autostart.desktop").is_file()
    assert re.search(r'^Exec=bash ".+/arm112-kiosk\.sh" "--server" "http://10\.0\.0\.5:8112"$', entry, re.M)


@posix
def test_linux_rejects_bad_server():
    code, out, err = run_sh("--dry-run", "--browser-bin", "/bin/true", "--server", "ftp://10.0.0.5")
    assert code != 0


def test_waiting_page_is_offline():
    """Страница ожидания открывается без сети: ни одного внешнего ресурса."""
    html = (KIOSK / "waiting.html").read_text(encoding="utf-8")
    assert not re.search(r'(src|href)\s*=\s*["\']?https?:', html)
    assert "@import" not in html and "url(" not in html
