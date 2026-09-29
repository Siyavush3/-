; Установщик тренажёра АРМ-112 для Windows (поставка, узел 8). Собирает tools/make_setup.py:
;   ISCC.exe /DAppVersion=<версия> /DStage=<папка сборки> /DOutDir=<куда> deploy\windows\setup.iss
; В папке сборки — программа, C-ядро, голоса, модель распознавания и встроенный Python 3.11 со всеми пакетами
; (python\): на целевой машине не нужны ни Python, ни интернет. После копирования файлов настройку делает
; deploy\windows\install.ps1 (база, администратор, эталон целостности, брандмауэр, автозапуск).
; Тихая установка: ARM112-Setup.exe /VERYSILENT /TASKS="service,firewall" /PORT=8112 [/ADMINPASSWORD=...]
; (без пароля — временный, он в var\logs\install.log; сменить при первом входе).
; Без прав администратора (показ на ноутбуке): /CURRENTUSER — в профиль пользователя, без автозапуска и брандмауэра.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Stage
  #error Укажите папку сборки: /DStage=... (tools/make_setup.py)
#endif
#ifndef OutDir
  #define OutDir "."
#endif
#ifndef MaxDirLen
  #define MaxDirLen "100"
#endif

[Setup]
AppId={{6F0C2A71-3B8E-4D5A-9E12-A112A112D0D5}
AppName=Тренажёр АРМ-112
AppVersion={#AppVersion}
AppVerName=Тренажёр АРМ-112 {#AppVersion}
AppPublisher=Команда АРМ-112 · ЛЦТ, задача 9
DefaultDirName={code:DefaultDir}
UsePreviousAppDir=yes
DirExistsWarning=no
DisableProgramGroupPage=yes
UninstallDisplayName=Тренажёр АРМ-112
OutputDir={#OutDir}
OutputBaseFilename=ARM112-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=4
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
CloseApplications=no
SetupLogging=yes

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "service"; Description: "Запускать сервер автоматически при старте Windows (задача планировщика от SYSTEM)"; Check: IsAdminInstallMode
Name: "firewall"; Description: "Открыть порт сервера в брандмауэре Windows для рабочих мест класса"; Check: IsAdminInstallMode
Name: "desktopicon"; Description: "Ярлык «Тренажёр АРМ-112» на рабочем столе"

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#ifdef VCRedist
; среда выполнения Visual C++ для озвучки (onnxruntime) — распространяемый пакет Microsoft
Source: "{#VCRedist}"; Flags: dontcopy
#endif

[Dirs]
Name: "{app}\var\logs"

[INI]
Filename: "{app}\open-trainer.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://127.0.0.1:{code:GetPort}/"

[Icons]
Name: "{autoprograms}\Тренажёр АРМ-112\Открыть тренажёр"; Filename: "{app}\open-trainer.url"
Name: "{autoprograms}\Тренажёр АРМ-112\Запустить сервер вручную"; Filename: "{app}\start-server.cmd"; WorkingDir: "{app}"
Name: "{autoprograms}\Тренажёр АРМ-112\Сопроводительная документация"; Filename: "{app}\docs\Сопроводительная документация.docx"
Name: "{autoprograms}\Тренажёр АРМ-112\Удалить тренажёр"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Тренажёр АРМ-112"; Filename: "{app}\open-trainer.url"; Tasks: desktopicon

[Run]
Filename: "{app}\start-server.cmd"; WorkingDir: "{app}"; Description: "Запустить сервер сейчас"; Flags: postinstall nowait skipifsilent runminimized; Check: not (IsAdminInstallMode and WizardIsTaskSelected('service'))
Filename: "{app}\open-trainer.url"; Description: "Открыть тренажёр в браузере"; Flags: postinstall shellexec nowait skipifsilent

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\deploy\windows\uninstall.ps1"" -Dir ""{app}"""; Flags: runhidden waituntilterminated; RunOnceId: "ARM112Stop"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\server"
Type: filesandordirs; Name: "{app}\pycore"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\deploy"
Type: filesandordirs; Name: "{app}\data"
Type: files; Name: "{app}\start-server.cmd"
Type: files; Name: "{app}\open-trainer.url"

[Code]
var
  PortPage: TInputQueryWizardPage;
  AdminPage: TInputQueryWizardPage;

function SetEnvironmentVariable(lpName: String; lpValue: String): Boolean;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

function DefaultDir(Param: String): String;
begin
  // для всех пользователей — C:\ARM112; только для себя (/CURRENTUSER) — в профиле, без прав администратора
  if IsAdminInstallMode then
    Result := 'C:\ARM112'
  else
    Result := ExpandConstant('{localappdata}\ARM112');
end;

function GetPort(Param: String): String;
begin
  Result := Trim(PortPage.Values[0]);
end;

function IsAscii(const S: String): Boolean;
var
  I: Integer;
begin
  Result := True;
  for I := 1 to Length(S) do
    if Ord(S[I]) > 127 then
      Result := False;
end;

function PasswordProblem(const P: String): String;
var
  I, C: Integer;
  Letter, Digit: Boolean;
begin
  Letter := False;
  Digit := False;
  for I := 1 to Length(P) do
  begin
    C := Ord(P[I]);
    if (C >= Ord('0')) and (C <= Ord('9')) then
      Digit := True
    else if ((C >= Ord('A')) and (C <= Ord('Z'))) or ((C >= Ord('a')) and (C <= Ord('z'))) or ((C >= $0400) and (C <= $04FF)) then
      Letter := True;
  end;
  if Length(P) < 8 then
    Result := 'Пароль короче 8 символов.'
  else if not (Letter and Digit) then
    Result := 'В пароле нужны и буквы, и цифры.'
  else
    Result := '';
end;

function Installed: Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\var\arm112.sqlite'));
end;

procedure InitializeWizard;
begin
  PortPage := CreateInputQueryPage(wpSelectDir, 'Сервер тренажёра',
    'На каком порту работать серверу?',
    'Рабочие места класса откроют тренажёр в браузере по адресу http://<адрес этого компьютера>:<порт>.');
  PortPage.Add('Порт:', False);
  PortPage.Values[0] := ExpandConstant('{param:PORT|' + GetPreviousData('Port', '8112') + '}');
  AdminPage := CreateInputQueryPage(PortPage.ID, 'Администратор тренажёра',
    'Пароль учётной записи admin',
    'Администратор заводит преподавателей и обучающихся. Пароль — не короче 8 символов, буквы и цифры.');
  AdminPage.Add('Пароль:', True);
  AdminPage.Add('Пароль ещё раз:', True);
  AdminPage.Values[0] := ExpandConstant('{param:ADMINPASSWORD|}');   // для тихой установки
  AdminPage.Values[1] := AdminPage.Values[0];
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'Port', GetPort(''));
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  // обновление поверх установленной версии: база и администратор уже есть
  Result := (PageID = AdminPage.ID) and Installed;
end;

function Refuse(const Text: String): Boolean;
begin
  // SuppressibleMsgBox: тихая установка (/VERYSILENT /SUPPRESSMSGBOXES) не ждёт человека — причина в журнале
  Log('Остановлено: ' + Text);
  SuppressibleMsgBox(Text, mbError, MB_OK, IDOK);
  Result := False;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Port: Integer;
  Problem: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    if not IsAscii(WizardDirValue) then
      Result := Refuse('Путь установки должен быть латиницей, например C:\ARM112: озвучка (espeak-ng) и сертификаты (openssl) не открывают пути с кириллицей.')
    else if Length(WizardDirValue) > {#MaxDirLen} then
      Result := Refuse('Путь установки слишком длинный (больше {#MaxDirLen} символов): файлы программы не поместятся в ограничение Windows на длину пути. Выберите покороче, например C:\ARM112.');
  end
  else if CurPageID = PortPage.ID then
  begin
    Port := StrToIntDef(GetPort(''), 0);
    if (Port < 1024) or (Port > 65535) then
      Result := Refuse('Порт — число от 1024 до 65535, например 8112.');
  end
  else if CurPageID = AdminPage.ID then
  begin
    if WizardSilent and (AdminPage.Values[0] = '') then
      Exit;  // тихая установка без /ADMINPASSWORD — временный пароль (он в var\logs\install.log)
    Problem := PasswordProblem(AdminPage.Values[0]);
    if AdminPage.Values[0] <> AdminPage.Values[1] then
      Problem := 'Пароли не совпадают.';
    if Problem <> '' then
      Result := Refuse(Problem);
  end;
end;

function PowerShell: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  Script: String;
begin
  // обновление: остановить работающий сервер, иначе файлы программы заняты
  Result := '';
  Script := ExpandConstant('{app}\deploy\windows\uninstall.ps1');
  if FileExists(Script) then
    Exec(PowerShell, '-NoProfile -ExecutionPolicy Bypass -File "' + Script + '" -Dir "' + ExpandConstant('{app}') + '" -StopOnly',
      '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Params, LogFile: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
#ifdef VCRedist
  if not FileExists(ExpandConstant('{sys}\msvcp140.dll')) then
  begin
    // 0 — установлено, 1638 — уже есть новее, 3010 — после перезагрузки; без пакета озвучка — текстом
    WizardForm.StatusLabel.Caption := 'Среда выполнения Visual C++ (нужна озвучке)…';
    ExtractTemporaryFile('vc_redist.x64.exe');
    Exec(ExpandConstant('{tmp}\vc_redist.x64.exe'), '/install /quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, Code);
  end;
#endif
  WizardForm.StatusLabel.Caption := 'Настройка сервера: база, администратор, эталон целостности, автозапуск…';
  LogFile := ExpandConstant('{app}\var\logs\install.log');
  Params := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\deploy\windows\install.ps1') + '"' +
    ' -Dir "' + ExpandConstant('{app}') + '" -Python "' + ExpandConstant('{app}\python\python.exe') + '"' +
    ' -Port ' + GetPort('') + ' -Unattended -Log "' + LogFile + '"';
  if not (IsAdminInstallMode and WizardIsTaskSelected('service')) then
    Params := Params + ' -NoService';
  if not (IsAdminInstallMode and WizardIsTaskSelected('firewall')) then
    Params := Params + ' -NoFirewall';
  // пароль — переменной окружения, а не в командной строке (она видна в списке процессов)
  if not Installed then
    SetEnvironmentVariable('A112_PASSWORD', AdminPage.Values[0]);
  Exec(PowerShell, Params, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Code);
  SetEnvironmentVariable('A112_PASSWORD', '');
  if Code <> 0 then
    SuppressibleMsgBox('Файлы установлены, но настройка сервера не завершилась (код ' + IntToStr(Code) + ').' + #13#10 +
      'Подробности — ' + LogFile, mbError, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and DirExists(ExpandConstant('{app}\var')) then
    if SuppressibleMsgBox('Удалить базу занятий и результатов, журналы и резервные копии (папка var)?' + #13#10 + #13#10 +
      '«Нет» — данные останутся в ' + ExpandConstant('{app}\var') + ' и подхватятся при повторной установке.',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
      DelTree(ExpandConstant('{app}\var'), True, True, True);
end;
