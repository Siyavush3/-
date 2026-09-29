@echo off
rem ARM-112 trainer: kiosk launcher for Windows. Details: README.md in this folder.
rem Arguments go to arm112-kiosk.ps1, e.g.:  arm112-kiosk.cmd -Server http://10.0.0.5:8112
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0arm112-kiosk.ps1" %*
if errorlevel 1 pause
