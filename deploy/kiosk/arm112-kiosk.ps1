<#
.SYNOPSIS
    Тренажёр ДДС: АРМ обучающегося в режиме киоска (Windows 10/11).

.DESCRIPTION
    Открывает АРМ в браузере на весь экран — без адресной строки, вкладок и меню.
    У киоска свой профиль браузера: режим включается, даже если обычный браузер уже открыт,
    и в киоске нет чужих вкладок, расширений и сохранённых данных.
    Пока сервер не отвечает, показывается страница ожидания; она сама перейдёт ко входу.
    Выход из киоска — Alt+F4.

    Адрес сервера и браузер берутся по порядку: ключи, переменные A112_SERVER и A112_BROWSER,
    файл kiosk.conf рядом со скриптом, значения по умолчанию (http://127.0.0.1:8112, auto).

.EXAMPLE
    .\arm112-kiosk.ps1 -Server http://10.0.0.5:8112

.EXAMPLE
    .\arm112-kiosk.ps1 -Install -Autostart -Server http://10.0.0.5:8112
    Ярлык «Тренажёр ДДС» на рабочем столе и запуск киоска при входе в Windows.

.EXAMPLE
    .\arm112-kiosk.ps1 -Window -DryRun
    Показать, какой браузер и с какими ключами будет запущен.
#>
[CmdletBinding()]
param(
    # Адрес сервера тренажёра
    [string]$Server,
    # auto | chrome | yandex | edge | firefox
    [string]$Browser,
    # Исполняемый файл браузера, если он установлен в нестандартное место
    [string]$BrowserPath,
    # Окно приложения вместо полного экрана — для настройки рабочего места
    [switch]$Window,
    # Дополнительные ключи браузера, например '--force-device-scale-factor=1'
    [string[]]$ExtraArgs = @(),
    # Создать ярлык «Тренажёр ДДС» (по умолчанию — на рабочем столе); киоск при этом не запускается
    [switch]$Install,
    # Запускать киоск при входе пользователя в Windows
    [switch]$Autostart,
    # Каталог для ярлыков вместо рабочего стола и автозагрузки
    [string]$ShortcutDir,
    # Только показать, что будет сделано
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
if ($DryRun) {
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
}

$cliServer = $Server
$cliBrowser = $Browser

function Read-Conf([string]$path) {
    $conf = @{}
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        foreach ($line in Get-Content -LiteralPath $path -Encoding UTF8) {
            $t = $line.Trim()
            if ($t -eq '' -or $t.StartsWith('#')) { continue }
            $i = $t.IndexOf('=')
            if ($i -gt 0) { $conf[$t.Substring(0, $i).Trim().ToUpperInvariant()] = $t.Substring($i + 1).Trim() }
        }
    }
    return $conf
}

$conf = Read-Conf (Join-Path $PSScriptRoot 'kiosk.conf')

function Pick([string]$cli, [string]$envName, [string]$confKey, [string]$default) {
    if ($cli) { return $cli }
    $fromEnv = [Environment]::GetEnvironmentVariable($envName)
    if ($fromEnv) { return $fromEnv }
    if ($conf.ContainsKey($confKey) -and $conf[$confKey]) { return $conf[$confKey] }
    return $default
}

$Server = (Pick $Server 'A112_SERVER' 'SERVER' 'http://127.0.0.1:8112').TrimEnd('/')
$Browser = (Pick $Browser 'A112_BROWSER' 'BROWSER' 'auto').ToLowerInvariant()

$uri = $null
if (-not [Uri]::TryCreate($Server, [UriKind]::Absolute, [ref]$uri) -or ($uri.Scheme -ne 'http' -and $uri.Scheme -ne 'https')) {
    throw "Адрес сервера должен начинаться с http:// или https://, а указан: $Server"
}
# Микрофон (вопросы заявителю голосом) браузер даёт только защищённым адресам: HTTPS или localhost.
# Сервер класса по http:// киоск объявляет защищённым — только его, и только в своём профиле.
$origin = $uri.Scheme + '://' + $uri.Authority
$insecure = $uri.Scheme -eq 'http' -and -not $uri.IsLoopback

# ---------- браузер ----------
function App-Path([string]$exe) {
    foreach ($root in 'HKLM:', 'HKCU:') {
        $item = Get-ItemProperty -LiteralPath "$root\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\$exe" -ErrorAction SilentlyContinue
        if ($item -and $item.'(default)') { return $item.'(default)'.Trim('"') }
    }
    return $null
}

$pf = $env:ProgramFiles
$pf86 = ${env:ProgramFiles(x86)}
$local = $env:LOCALAPPDATA
# Порядок для auto: Chrome и Яндекс Браузер названы в ТЗ, Edge есть на любой Windows 10/11, Firefox — последним:
# у него нет окна-приложения, а автозапуск звука включается настройкой профиля.
$known = [ordered]@{
    chrome  = @((App-Path 'chrome.exe'), "$pf\Google\Chrome\Application\chrome.exe",
                "$pf86\Google\Chrome\Application\chrome.exe", "$local\Google\Chrome\Application\chrome.exe")
    yandex  = @("$local\Yandex\YandexBrowser\Application\browser.exe", "$pf\Yandex\YandexBrowser\Application\browser.exe",
                "$pf86\Yandex\YandexBrowser\Application\browser.exe")
    edge    = @((App-Path 'msedge.exe'), "$pf86\Microsoft\Edge\Application\msedge.exe", "$pf\Microsoft\Edge\Application\msedge.exe")
    firefox = @((App-Path 'firefox.exe'), "$pf\Mozilla Firefox\firefox.exe", "$pf86\Mozilla Firefox\firefox.exe")
}

function Find-Browser([string]$kind) {
    foreach ($p in $known[$kind]) {
        if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { return $p }
    }
    return $null
}

if ($BrowserPath) {
    if (-not (Test-Path -LiteralPath $BrowserPath -PathType Leaf)) { throw "Не найден файл браузера: $BrowserPath" }
    $exe = $BrowserPath
    if ($Browser -eq 'auto') {
        if ($BrowserPath -match 'firefox') { $Browser = 'firefox' } else { $Browser = 'chrome' }
    }
} elseif ($Browser -eq 'auto') {
    $exe = $null
    foreach ($kind in @($known.Keys)) {
        $exe = Find-Browser $kind
        if ($exe) { $Browser = $kind; break }
    }
    if (-not $exe) { throw 'Не найден ни один браузер: Chrome, Яндекс Браузер, Edge или Firefox. Укажите путь ключом -BrowserPath.' }
} else {
    if (-not $known.Contains($Browser)) { throw "Неизвестный браузер «$Browser»: chrome, yandex, edge, firefox или auto." }
    $exe = Find-Browser $Browser
    if (-not $exe) { throw "Браузер «$Browser» не найден. Укажите путь ключом -BrowserPath." }
}

# ---------- что открыть ----------
# Страница ожидания лежит на рабочем месте: открывается даже при выключенном сервере.
$page = Join-Path $PSScriptRoot 'waiting.html'
$startUrl = (New-Object System.Uri $page).AbsoluteUri + '?server=' + [Uri]::EscapeDataString($Server)
$profileDir = Join-Path $env:LOCALAPPDATA ('arm112\kiosk-' + $Browser)

if ($Browser -eq 'firefox') {
    $argv = @('--new-instance', '--profile', $profileDir)
    if (-not $Window) { $argv += '--kiosk' }
    $argv += $ExtraArgs
    $argv += $startUrl
} else {
    $argv = @("--user-data-dir=$profileDir", '--no-first-run', '--no-default-browser-check',
              '--disable-features=Translate', '--disable-session-crashed-bubble', '--hide-crash-restore-bubble',
              '--noerrdialogs', '--disable-pinch', '--overscroll-history-navigation=0',
              # доклады бригады звучат без предварительного клика по странице
              '--autoplay-policy=no-user-gesture-required',
              # микрофон оператора — без вопроса о разрешении
              '--use-fake-ui-for-media-stream')
    if ($insecure) { $argv += "--unsafely-treat-insecure-origin-as-secure=$origin" }
    $argv += $ExtraArgs
    if ($Window) {
        $argv += @('--start-maximized', "--app=$startUrl")
    } else {
        $argv += '--kiosk'
        if ($Browser -eq 'edge') { $argv += '--edge-kiosk-type=fullscreen' }
        $argv += $startUrl
    }
}

# Кавычки по правилам разбора командной строки Windows (CommandLineToArgvW)
function Quote-Arg([string]$a) {
    if ($a -ne '' -and $a -notmatch '[\s"]') { return $a }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    $slashes = 0
    foreach ($ch in $a.ToCharArray()) {
        if ($ch -eq '\') { $slashes++; continue }
        if ($ch -eq '"') {
            [void]$sb.Append('\' * ($slashes * 2 + 1)).Append('"')
        } else {
            [void]$sb.Append('\' * $slashes).Append($ch)
        }
        $slashes = 0
    }
    [void]$sb.Append('\' * ($slashes * 2)).Append('"')
    return $sb.ToString()
}

function Join-Args([string[]]$list) {
    return (($list | ForEach-Object { Quote-Arg $_ }) -join ' ')
}

$cmdline = Join-Args $argv

# ---------- ярлыки ----------
# WScript.Shell пишет ярлык в кодировке ANSI: на английской Windows кириллица в пути и аргументах
# превращается в «???». Поэтому ярлык создаётся через Unicode-интерфейс IShellLinkW.
$shellLinkSource = @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace Arm112Kiosk {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    class CShellLink { }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("000214F9-0000-0000-C000-000000000046")]
    interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file, int cch, IntPtr fd, uint flags);
        void GetIDList(out IntPtr pidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder name, int cch);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string name);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder dir, int cch);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string dir);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int cch);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
        void GetHotkey(out short hotkey);
        void SetHotkey(short hotkey);
        void GetShowCmd(out int showCmd);
        void SetShowCmd(int showCmd);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int cch, out int icon);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int icon);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pathRel, uint reserved);
        void Resolve(IntPtr hwnd, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string file);
    }

    public static class Shortcut {
        public static void Save(string path, string target, string args, string workDir, string icon, string description) {
            IShellLinkW link = (IShellLinkW)new CShellLink();
            link.SetPath(target);
            link.SetArguments(args);
            link.SetWorkingDirectory(workDir);
            link.SetIconLocation(icon, 0);
            link.SetDescription(description);
            ((IPersistFile)link).Save(path, true);
        }
    }
}
'@

function Save-Shortcut([string]$path, [string]$target, [string]$arguments, [string]$workDir, [string]$icon, [string]$description) {
    try {
        if (-not ('Arm112Kiosk.Shortcut' -as [type])) { Add-Type -TypeDefinition $shellLinkSource -Language CSharp }
        [Arm112Kiosk.Shortcut]::Save($path, $target, $arguments, $workDir, $icon, $description)
    } catch {
        # Add-Type запрещён политикой (режим ограниченного языка) — старый путь; кириллица сохранится
        # только если она есть в кодовой странице системы (на русской Windows — есть).
        Write-Warning "IShellLinkW недоступен ($($_.Exception.Message)); ярлык создаётся через WScript.Shell"
        $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
        $lnk.TargetPath = $target
        $lnk.Arguments = $arguments
        $lnk.WorkingDirectory = $workDir
        $lnk.IconLocation = "$icon,0"
        $lnk.Description = $description
        $lnk.Save()
    }
}
if ($Install -or $Autostart) {
    $psArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $PSCommandPath)
    if ($cliServer) { $psArgs += @('-Server', $cliServer) }
    if ($cliBrowser) { $psArgs += @('-Browser', $cliBrowser) }
    if ($BrowserPath) { $psArgs += @('-BrowserPath', $BrowserPath) }
    if ($Window) { $psArgs += '-Window' }
    foreach ($x in $ExtraArgs) { $psArgs += @('-ExtraArgs', $x) }

    $targets = @()
    if ($Install) {
        $dir = $ShortcutDir
        if (-not $dir) { $dir = [Environment]::GetFolderPath('Desktop') }
        $targets += Join-Path $dir 'Тренажёр ДДС.lnk'
    }
    if ($Autostart) {
        $dir = $ShortcutDir
        if (-not $dir) { $dir = [Environment]::GetFolderPath('Startup') }
        $targets += Join-Path $dir 'Тренажёр ДДС — автозапуск.lnk'
    }
    $target = Join-Path $PSHOME 'powershell.exe'
    $lnkArgs = Join-Args $psArgs
    $description = 'Тренажёр оператора ДДС: АРМ-112 в режиме киоска'
    foreach ($lnkPath in $targets) {
        if ($DryRun) { Write-Output "shortcut: $lnkPath"; continue }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $lnkPath) | Out-Null
        Save-Shortcut $lnkPath $target $lnkArgs $PSScriptRoot "$exe" $description
        Write-Output "Создан ярлык: $lnkPath"
    }
    return
}

# ---------- запуск ----------
if ($DryRun) {
    Write-Output "browser: $Browser"
    Write-Output "exe: $exe"
    Write-Output "profile: $profileDir"
    Write-Output "start: $startUrl"
    Write-Output "command: $(Quote-Arg $exe) $cmdline"
    return
}

if ($Browser -eq 'firefox') {
    New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
    # Настройки профиля киоска: без приветствий и восстановления сессии, звук без клика, микрофон без вопроса
    Set-Content -LiteralPath (Join-Path $profileDir 'user.js') -Encoding ASCII -Value @(
        'user_pref("browser.shell.checkDefaultBrowser", false);',
        'user_pref("browser.aboutwelcome.enabled", false);',
        'user_pref("browser.startup.homepage_override.mstone", "ignore");',
        'user_pref("browser.sessionstore.resume_from_crash", false);',
        'user_pref("browser.translations.enable", false);',
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
        'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);',
        'user_pref("media.autoplay.default", 0);',
        'user_pref("media.autoplay.blocking_policy", 0);',
        'user_pref("media.navigator.permission.disabled", true);'
    ) + $(if ($insecure) { @(
        "user_pref(`"dom.securecontext.allowlist`", `"$($uri.Host)`");",
        'user_pref("media.devices.insecure.enabled", true);',
        'user_pref("media.getusermedia.insecure.enabled", true);') } else { @() })
}
Start-Process -FilePath $exe -ArgumentList $cmdline | Out-Null
Write-Output "Киоск: $Browser, сервер $Server"
