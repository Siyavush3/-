<#
.SYNOPSIS
    Остановка и снятие сервера тренажёра АРМ-112: процессы сервера, задача автозапуска, правило брандмауэра.

.DESCRIPTION
    Вызывается деинсталлятором ARM112-Setup.exe и перед обновлением поверх установленной версии (-StopOnly):
    сервер останавливается, чтобы файлы программы не были заняты. База, журналы и резервные копии (папка var)
    этот скрипт не трогает — их судьбу решает деинсталлятор, спросив администратора.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1 -Dir C:\ARM112
#>
param(
    [string]$Dir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [switch]$StopOnly
)
$Task = 'ARM112-Server'

# 1. задача автозапуска: остановить (без снятия при -StopOnly — после обновления её запустят снова)
$t = Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
if ($t) {
    Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
    if (-not $StopOnly) { Unregister-ScheduledTask -TaskName $Task -Confirm:$false -ErrorAction SilentlyContinue }
}

# 2. процессы сервера и озвучки из этой папки: остановка задачи не завершает запущенный ею python.exe
$prefixes = @((Join-Path $Dir 'python\').ToLowerInvariant(), (Join-Path $Dir '.venv\').ToLowerInvariant())
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $p = $_.ExecutablePath; $p -and ($prefixes | Where-Object { $p.ToLowerInvariant().StartsWith($_) }) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 3. правило брандмауэра
if (-not $StopOnly) {
    Get-NetFirewallRule -DisplayName 'ARM112 trainer' -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
}
exit 0
