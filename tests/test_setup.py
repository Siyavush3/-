"""Установщик ARM112-Setup.exe (поставка, узел 8): скрипт Inno Setup, встроенный Python, PowerShell-скрипты."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
windows = pytest.mark.skipif(os.name != "nt", reason="PowerShell и PE-файлы Windows")


def test_deploy_scripts_have_no_control_characters():
    """Управляющий символ в скрипте ломает его молча: «\\v» в пути {tmp}\\vc_redist.x64.exe однажды стал
    вертикальной табуляцией — установщик не нашёл бы пакет Visual C++."""
    for f in list(DEPLOY.rglob("*")) + list((ROOT / "tools").glob("*.py")):
        if f.is_file() and f.suffix in (".ps1", ".iss", ".sh", ".cmd", ".py", ".conf", ".service", ".md"):
            text = f.read_bytes().decode("utf-8-sig")
            bad = [c for c in text if ord(c) < 32 and c not in "\r\n\t"]
            assert not bad, (f, bad)


def test_inno_script_encoding_and_references():
    raw = (DEPLOY / "windows" / "setup.iss").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")               # без BOM Inno Setup читает скрипт в кодировке системы
    text = raw.decode("utf-8-sig")
    for script in ("install.ps1", "uninstall.ps1"):
        assert "deploy\\windows\\" + script in text and (DEPLOY / "windows" / script).is_file()
    assert "{param:PORT|" in text and "{param:ADMINPASSWORD|}" in text   # тихая установка
    assert "SuppressibleMsgBox" in text and "MsgBox(" not in text.replace("SuppressibleMsgBox(", "")
    assert "PrivilegesRequiredOverridesAllowed=commandline" in text


def test_installer_accepts_embedded_python_and_password_from_setup():
    text = (DEPLOY / "windows" / "install.ps1").read_bytes().decode("utf-8-sig")
    assert "[string]$Python" in text and "[string]$Log" in text and "Start-Transcript" in text
    assert "$plain = $env:A112_PASSWORD" in text          # пароль из мастера — переменной окружения


def test_embedded_python_finds_server_modules():
    """Встроенный Python не читает PYTHONPATH: модули сервера — строками ._pth (процесс озвучки -m speech.render)."""
    import make_setup
    assert r"..\server" in make_setup.PTH and r"..\pycore" in make_setup.PTH and "import site" in make_setup.PTH
    assert make_setup.EMBED.startswith("3.11.") and len(make_setup.EMBED_SHA256) == 64


@windows
def test_pe_import_reader():
    import make_setup
    names = [n.lower() for n in make_setup.pe_imports(Path(sys.base_prefix) / "python.exe")]   # .venv\python.exe — перенаправитель
    assert any(n.startswith("python3") for n in names) and "kernel32.dll" in names
    assert make_setup.pe_imports(ROOT / "README.md") == []


@windows
def test_powershell_scripts_parse():
    scripts = [DEPLOY / "windows" / "install.ps1", DEPLOY / "windows" / "uninstall.ps1", DEPLOY / "kiosk" / "arm112-kiosk.ps1"]
    for s in scripts:
        cmd = ("$e = $null; [System.Management.Automation.Language.Parser]::ParseFile('%s', [ref]$null, [ref]$e) | Out-Null; "
               "$e.Count" % s)
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
        assert out.stdout.strip() == "0", (s, out.stdout, out.stderr)
