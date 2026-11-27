; Установщик Kat (Inno Setup 6/7). Собирается из build.bat:
;   ISCC.exe /DAppVersion=2.0.0 installer\Kat.iss  ->  dist\KatSetup-2.0.0.exe
; Ставит программу для текущего пользователя, без прав администратора.
; Модели (~2 ГБ) и библиотеки видеокарты Kat.exe докачивает сам при первом запуске.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

; Тестовая сборка (ISCC /DTestBuild ...): своя метка установки и папка — проверка
; установщика никогда не задевает настоящий установленный Kat.
#ifdef TestBuild
  #define MyAppId "{{6F2C7E1A-4B8D-4C1E-9A57-2D3B8E6F4A99}"
  #define MyName "Kat TEST"
  #define OutName "KatSetup-test"
  #define MyDir "KatTest"
#else
  #define MyAppId "{{6F2C7E1A-4B8D-4C1E-9A57-2D3B8E6F4A10}"
  #define MyName "Kat — голосовой переводчик"
  #define OutName "KatSetup-" + AppVersion
  #define MyDir "Kat"
#endif

[Setup]
AppId={#MyAppId}
AppName=Kat
AppVersion={#AppVersion}
AppVerName=Kat {#AppVersion}
AppPublisher=L0lik
AppPublisherURL=https://github.com/L0lik909/voice-translator
AppSupportURL=https://github.com/L0lik909/voice-translator/issues
AppUpdatesURL=https://github.com/L0lik909/voice-translator/releases
DefaultDirName={localappdata}\Programs\{#MyDir}
DisableDirPage=auto
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename={#OutName}
SetupIconFile=Kat.ico
UninstallDisplayIcon={app}\Kat.exe
UninstallDisplayName={#MyName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
; Kat закрываем сами (см. [Code]) — Restart Manager с иконкой в трее не справляется
CloseApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Ярлыки:"
Name: "startmenuicon"; Description: "Ярлык в меню «Пуск»"; GroupDescription: "Ярлыки:"

[Files]
Source: "..\dist\Kat\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; words.txt — подсказки, которые пользователь мог дописать: при обновлении не трогаем
Source: "..\words.txt"; DestDir: "{app}"; Flags: onlyifdoesntexist
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Kat"; Filename: "{app}\Kat.exe"; Comment: "Kat — голосовой переводчик для игр"; Tasks: startmenuicon
Name: "{autodesktop}\Kat"; Filename: "{app}\Kat.exe"; Comment: "Kat — голосовой переводчик для игр"; Tasks: desktopicon

[Registry]
; автозапуск, если его включали в настройках Kat, — убрать вместе с программой
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "VoiceTranslator"; Flags: uninsdeletevalue dontcreatekey

[Run]
; обычная установка — галочка «Запустить Kat» на последней странице
Filename: "{app}\Kat.exe"; Description: "Запустить Kat"; Flags: nowait postinstall skipifsilent
; обновление из самого Kat (тихая установка с /LAUNCH=1) — сразу запустить новую версию
Filename: "{app}\Kat.exe"; Flags: nowait; Check: LaunchAfterSilent

[UninstallDelete]
; всё, что Kat создал сам: модели, библиотеки видеокарты, настройки, лог
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\cuda"
Type: files; Name: "{app}\settings.ini"
Type: files; Name: "{app}\settings.ini.tmp"
Type: files; Name: "{app}\glossary.txt"
Type: files; Name: "{app}\words.txt"
Type: files; Name: "{app}\log.txt"
Type: files; Name: "{app}\log.old.txt"
Type: files; Name: "{app}\last.wav"
Type: files; Name: "{app}\icon-*.ico"
Type: files; Name: "{app}\version.txt"
Type: files; Name: "{app}\migrate_from.txt"
Type: dirifempty; Name: "{app}"

[Code]
procedure CloseKat();
var
  Code: Integer;
  P: String;
begin
  { Kat живёт в трее — закрываем его перед заменой файлов. Только ЭТОТ Kat.exe
    (из папки установки), а не все Kat.exe на компьютере. }
  P := ExpandConstant('{app}\Kat.exe');
  StringChangeEx(P, '''', '''''', True);
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -Command "Get-CimInstance Win32_Process -Filter ''Name=''''Kat.exe'''''' | ' +
    'Where-Object { $_.ExecutablePath -eq ''' + P + ''' } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"',
    '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(500);
end;

{ Прежняя копия Kat из исходников (start.bat): ищем по её ярлыку — в нём путь к
  translator.py. Оттуда Kat.exe при первом запуске перенесёт настройки и модели. }
function OldKatDir(): String;
var
  Shell, Lnk: Variant;
  Names: array of String;
  I: Integer;
  Script: String;
begin
  Result := '';
  SetArrayLength(Names, 4);
  Names[0] := ExpandConstant('{userdesktop}\Kat.lnk');
  Names[1] := ExpandConstant('{userprograms}\Kat.lnk');
  Names[2] := ExpandConstant('{userdesktop}\Голосовой переводчик.lnk');
  Names[3] := ExpandConstant('{userprograms}\Голосовой переводчик.lnk');
  try
    Shell := CreateOleObject('WScript.Shell');
  except
    Exit;
  end;
  for I := 0 to GetArrayLength(Names) - 1 do
    if FileExists(Names[I]) then
    begin
      Lnk := Shell.CreateShortcut(Names[I]);
      Script := RemoveQuotes(Trim(Lnk.Arguments));
      if (CompareText(ExtractFileName(Script), 'translator.py') = 0) and
         FileExists(AddBackslash(ExtractFileDir(Script)) + 'settings.ini') then
      begin
        Result := ExtractFileDir(Script);
        Exit;
      end;
    end;
end;

procedure CloseOldKat();
var
  Code: Integer;
begin
  { прежняя копия работает как pythonw.exe translator.py — закрываем только её }
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -Command "Get-CimInstance Win32_Process -Filter ''Name=''''pythonw.exe'''''' | ' +
    'Where-Object { $_.CommandLine -like ''*translator.py*'' } | ' +
    'ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"',
    '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Old: String;
  Lines: TArrayOfString;
begin
  if (CurStep = ssInstall) and not FileExists(ExpandConstant('{app}\settings.ini')) then
  begin
    Old := OldKatDir();
    if Old <> '' then
    begin
      Log('Prev Kat: ' + Old);
      ForceDirectories(ExpandConstant('{app}'));
      SetArrayLength(Lines, 1);
      Lines[0] := Old;
      SaveStringsToUTF8File(ExpandConstant('{app}\migrate_from.txt'), Lines, False);
      { /KEEPOLD=1 — не закрывать прежнюю копию (для проверки установщика) }
      if ExpandConstant('{param:KEEPOLD|0}') <> '1' then
        CloseOldKat();
    end;
  end;
end;

function LaunchAfterSilent(): Boolean;
begin
  Result := WizardSilent() and (ExpandConstant('{param:LAUNCH|0}') = '1');
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  CloseKat();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    CloseKat();
end;
