; Установщик Kat (Inno Setup 6/7). Собирается из build.bat:
;   ISCC.exe /DAppVersion=2.0.0 installer\Kat.iss  ->  dist\KatSetup-2.0.0.exe
; Ставит программу для текущего пользователя, без прав администратора.
; Модели (~2 ГБ) и библиотеки видеокарты Kat.exe докачивает сам при первом запуске.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F2C7E1A-4B8D-4C1E-9A57-2D3B8E6F4A10}
AppName=Kat
AppVersion={#AppVersion}
AppVerName=Kat {#AppVersion}
AppPublisher=L0lik909
AppPublisherURL=https://github.com/L0lik909/voice-translator
AppSupportURL=https://github.com/L0lik909/voice-translator/issues
AppUpdatesURL=https://github.com/L0lik909/voice-translator/releases
DefaultDirName={localappdata}\Programs\Kat
DisableDirPage=auto
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=KatSetup-{#AppVersion}
SetupIconFile=Kat.ico
UninstallDisplayIcon={app}\Kat.exe
UninstallDisplayName=Kat — голосовой переводчик
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
Type: dirifempty; Name: "{app}"

[Code]
procedure CloseKat();
var
  Code: Integer;
begin
  { Kat живёт в трее — закрываем его перед заменой файлов }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM Kat.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(500);
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
