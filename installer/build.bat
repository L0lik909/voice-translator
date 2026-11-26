@echo off
rem Сборка установщика Kat: dist\KatSetup-<версия>.exe
rem Нужно: Python 3.10 (py -3.10) и Inno Setup 6 или 7 (jrsoftware.org).
rem ВАЖНО: файл с переводами строк Windows (CRLF), см. .gitattributes.
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=.build-venv\Scripts\python.exe"

if exist "%PY%" goto have_venv
echo Создаю окружение для сборки...
py -3.10 -m venv .build-venv
if errorlevel 1 goto fail
"%PY%" -m pip install -q --upgrade pip
:have_venv
echo Ставлю компоненты сборки...
"%PY%" -m pip install -q -r installer\requirements-build.txt
if errorlevel 1 goto fail

for /f "usebackq delims=" %%v in (`"%PY%" installer\version.py`) do set "VER=%%v"
echo Версия: %VER%

"%PY%" installer\make_icon.py installer\Kat.ico
if errorlevel 1 goto fail

echo Собираю Kat.exe...
"%PY%" -m PyInstaller --noconfirm --clean installer\Kat.spec --distpath dist --workpath build\pyinstaller
if errorlevel 1 goto fail

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 7\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 7\ISCC.exe"
if not exist "%ISCC%" goto no_inno

echo Собираю установщик...
"%ISCC%" /Q /DAppVersion=%VER% installer\Kat.iss
if errorlevel 1 goto fail
echo.
echo Готово: dist\KatSetup-%VER%.exe
exit /b 0

:no_inno
echo [!] Не найден Inno Setup. Установите его с https://jrsoftware.org/isdl.php
exit /b 1

:fail
echo [!] Сборка не удалась - см. сообщения выше.
exit /b 1
