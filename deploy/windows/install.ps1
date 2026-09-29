<#
.SYNOPSIS
    Установка сервера тренажёра АРМ-112 на Windows 10/11 или Windows Server (изолированный контур).

.DESCRIPTION
    Запускается из распакованного дистрибутива (tools/make_dist.py) от имени администратора — или из
    ARM112-Setup.exe (tools/make_setup.py) после копирования файлов, с готовым встроенным Python (-Python):
      1. проверяет Python 3.11 (ставит его из дистрибутива, если там лежит python-3.11*-amd64.exe);
      2. копирует программу в папку установки (путь — латиницей: espeak-ng и openssl не открывают кириллицу);
      3. создаёт окружение .venv и ставит пакеты из папки wheels без интернета;
      4. создаёт или обновляет базу, учётную запись администратора, эталон целостности;
      5. при -Tls создаёт сертификат для адреса сервера;
      6. открывает порт в брандмауэре и регистрирует автозапуск задачей планировщика (от SYSTEM,
         перезапуск при сбое) — сторонние службы-обёртки не нужны.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -Tls -ServerName 10.0.0.5 -Db "postgresql://arm112:пароль@localhost/arm112"
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -DryRun
#>
param(
    [string]$Dir = "C:\ARM112",
    [int]$Port = 8112,
    [string]$Bind = "0.0.0.0",
    [string]$Db = "",
    [switch]$Tls,
    [string]$ServerName = "",
    [string]$AdminLogin = "admin",
    [switch]$NoService,
    [switch]$NoFirewall,
    [switch]$Unattended,
    [string]$Python = "",   # готовый Python со всеми пакетами (встроенный в ARM112-Setup.exe): без .venv и pip
    [string]$Log = "",      # весь вывод — ещё и в файл (Setup.exe запускает установку без окна)
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
if ($Log) { Start-Transcript -LiteralPath $Log -Force | Out-Null }
trap { if ($Log) { Write-Host ('ОШИБКА: ' + $_); Stop-Transcript | Out-Null }; exit 1 }
$env:PYTHONUTF8 = '1'  # pip и manage.py читают файлы в UTF-8 при любой кодировке системы (cp1251, cp1252)
$Src = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Task = 'ARM112-Server'

function Step([string]$text) { Write-Host ('== ' + $text) -ForegroundColor Cyan }
function Run([string]$what, [scriptblock]$action) {
    if ($DryRun) { Write-Host ('   [проверка] ' + $what) } else { Write-Host ('   ' + $what); & $action }
}

Step 'Проверка'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin -and -not $DryRun -and (-not $NoService -or -not $NoFirewall)) {
    throw 'Нужны права администратора (служба и брандмауэр). Запустите PowerShell от имени администратора или укажите -NoService -NoFirewall.'
}
if ($Dir -match '[^\x00-\x7F]') {
    throw "Путь установки должен быть латиницей: '$Dir'. espeak-ng (озвучка) и openssl (сертификаты) не открывают пути с кириллицей."
}

function Find-Python {
    foreach ($cand in @(@('py', '-3.11'), @('python'), @('py', '-3'))) {
        $pre = if ($cand.Count -gt 1) { @($cand[1..($cand.Count - 1)]) } else { @() }
        try {
            # без кавычек внутри кода: PowerShell 5.1 теряет вложенные кавычки в аргументах внешних программ
            $v = & $cand[0] @($pre + @('-c', 'import sys; print(*sys.version_info[:2], sep=chr(46))')) 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]'3.10') {
                $exe = & $cand[0] @($pre + @('-c', 'import sys; print(sys.executable)'))
                return @{ Exe = $exe; Version = $v }
            }
        } catch { }
    }
    return $null
}
$py = if ($Python) { @{ Exe = $Python; Version = 'встроенный' } } else { Find-Python }
if (-not $py) {
    $installer = Get-ChildItem -Path $Src -Filter 'python-3.11*-amd64.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $installer) { throw 'Нет Python 3.10+ и нет установщика python-3.11*-amd64.exe в дистрибутиве.' }
    Run "установка $($installer.Name)" { Start-Process -Wait -FilePath $installer.FullName -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_launcher=1' }
    if (-not $DryRun) { $py = Find-Python; if (-not $py) { throw 'Python не установился.' } }
}
if ($py) { Write-Host "   Python $($py.Version): $($py.Exe)" }

Step "Файлы программы → $Dir"
if ((Resolve-Path -LiteralPath $Src).Path -ne $Dir) {
    Run "копирование из $Src" {
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        robocopy $Src $Dir /E /NFL /NDL /NJH /NJS /NP /XD .venv var .git __pycache__ | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy: код $LASTEXITCODE" }
    }
}
$venvPy = if ($Python) { $Python } else { Join-Path $Dir '.venv\Scripts\python.exe' }

if (-not $Python) {
Step 'Окружение Python и пакеты'
Run 'создание .venv' { & $py.Exe -m venv (Join-Path $Dir '.venv') }
$wheels = Join-Path $Dir 'wheels'
$offline = @()
if (Test-Path $wheels) { $offline = @('--no-index', '--find-links', $wheels) } else { Write-Host '   нет папки wheels — пакеты из интернета' }
function Pip([string]$req) {
    # вывод pip — на экран: функция PowerShell возвращает весь свой вывод, а нужен только код завершения
    & $venvPy -m pip install --disable-pip-version-check @offline -r (Join-Path $Dir "requirements\$req") | Out-Host
    return $LASTEXITCODE
}
Run 'пакеты сервера' { if (Pip 'server.txt') { throw 'pip: пакеты сервера не установлены' } }
$voices = @(Get-ChildItem -Path (Join-Path $Dir 'data\voices') -Filter '*.onnx' -ErrorAction SilentlyContinue)
if ($voices.Count) {
    $msg = "пакеты озвучки (голосов: $($voices.Count))"
    Run $msg { if (Pip 'tts.txt') { Write-Warning 'озвучка не установлена — реплики будут текстом' } }
}
if (Get-ChildItem -Path (Join-Path $Dir 'data\asr') -Filter 'vosk-model-*.zip' -ErrorAction SilentlyContinue) {
    Run 'распознавание речи (Vosk)' {
        if (Pip 'asr.txt') { Write-Warning 'распознавание речи не установлено — вопросы кнопками и текстом' }
        # модель: сверка SHA-256 архива и распаковка в data\asr
        & $venvPy (Join-Path $Dir 'tools\fetch_asr.py') --check | Out-Host
        if ($LASTEXITCODE) { Write-Warning 'модель распознавания повреждена — вопросы кнопками и текстом' }
    }
}
}  # конец установки пакетов (со встроенным Python они уже на месте)

Step 'База данных и учётные записи'
$envLines = @('@echo off', 'rem ARM-112 trainer server (created by deploy\windows\install.ps1)', 'set PYTHONIOENCODING=utf-8', 'set PYTHONUTF8=1')
if ($Db) { $envLines += "set A112_DB=$Db"; $env:A112_DB = $Db }
Run 'схема базы' { & $venvPy (Join-Path $Dir 'server\manage.py') init-db }
if (-not $DryRun) {
    $exists = & $venvPy (Join-Path $Dir 'server\manage.py') list-users | Select-String -Pattern "^$AdminLogin\s" -Quiet
    if (-not $exists) {
        $plain = $env:A112_PASSWORD  # Setup.exe передаёт пароль из мастера переменной окружения
        if (-not $plain -and -not $Unattended) {  # пароль спрашивает PowerShell и передаёт переменной окружения — не в командной строке
            $sec = Read-Host -AsSecureString "   Пароль администратора «$AdminLogin» (Enter — временный, со сменой при первом входе)"
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
        }
        if ($plain) { $env:A112_PASSWORD = $plain; $mode = @() } else { $mode = @('--temporary') }
        & $venvPy (Join-Path $Dir 'server\manage.py') create-user $AdminLogin --name 'Администратор' --role admin @mode
        Remove-Item Env:A112_PASSWORD -ErrorAction SilentlyContinue
        if ($LASTEXITCODE) { throw 'учётная запись администратора не создана' }
    }
}

$runArgs = "--host $Bind --port $Port"
if ($Tls) {
    Step 'Сертификат TLS'
    $name = if ($ServerName) { $ServerName } else { $env:COMPUTERNAME }
    Run "make-cert для $name" { & $venvPy (Join-Path $Dir 'server\manage.py') make-cert --host $name }
    $runArgs += " --certfile var\tls\arm112.crt --keyfile var\tls\arm112.key"
}
$envLines += "cd /d `"$Dir`"", "`"$venvPy`" server\run.py $runArgs"
$starter = Join-Path $Dir 'start-server.cmd'
Run "запуск: $starter" { Set-Content -LiteralPath $starter -Value $envLines -Encoding OEM }
Run 'эталон целостности' { & $venvPy (Join-Path $Dir 'server\manage.py') integrity --write }

if (-not $NoFirewall) {
    Step "Брандмауэр: входящие TCP $Port"
    Run 'правило ARM112' {
        Get-NetFirewallRule -DisplayName 'ARM112 trainer' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        New-NetFirewallRule -DisplayName 'ARM112 trainer' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
    }
}
if (-not $NoService) {
    Step "Автозапуск: задача планировщика $Task"
    Run 'регистрация и запуск' {
        # повторная установка (обновление, включение TLS): работающий сервер держит порт — остановить
        & (Join-Path $PSScriptRoot 'uninstall.ps1') -Dir $Dir -StopOnly
        $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$starter`"" -WorkingDirectory $Dir
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName $Task
    }
}

$scheme = if ($Tls) { 'https' } else { 'http' }
Step 'Готово'
Write-Host "   адрес для класса: ${scheme}://<адрес сервера>:$Port   (на самом сервере: ${scheme}://127.0.0.1:$Port)"
if ($NoService) { Write-Host "   запуск вручную: $starter" }
else { Write-Host "   остановить/запустить: Stop-ScheduledTask $Task / Start-ScheduledTask $Task" }
Write-Host "   учётные записи преподавателей и обучающихся заводит администратор в веб-интерфейсе"
Write-Host "   рабочие места обучающихся: deploy\kiosk (режим киоска), установка — deploy\kiosk\README.md"
if ($Log) { Stop-Transcript | Out-Null }
