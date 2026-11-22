@echo off
rem Установка и обновление Kat (голосовой переводчик).
rem Нужен только при первом запуске и после обновления программы -
rem в остальное время запускайте ярлык "Kat"
rem на рабочем столе или в меню "Пуск".
rem ВАЖНО: файл должен быть с переводами строк Windows (CRLF),
rem иначе cmd режет русские строки посередине.
chcp 65001 >nul
cd /d "%~dp0"
title Kat - установка

if exist "venv\Scripts\pythonw.exe" goto have_venv

echo Первый запуск: создаю окружение...
rem Python: сначала "python", если его нет - лаунчер "py"
set "PY=python"
python --version >nul 2>&1
if errorlevel 1 set "PY=py -3"
%PY% --version >nul 2>&1
if errorlevel 1 goto no_python
%PY% -m venv venv
if errorlevel 1 goto no_python
venv\Scripts\python -m pip install --upgrade pip

:have_venv
rem Зависимости ставим, только если requirements.txt изменился с прошлой установки
set "STAMP=venv\requirements.sha256"
set "REQ="
for /f "usebackq delims=" %%h in (`venv\Scripts\python -c "import hashlib;print(hashlib.sha256(open(r'requirements.txt','rb').read()).hexdigest())"`) do set "REQ=%%h"
set "OLD="
if exist "%STAMP%" set /p OLD=<"%STAMP%"
if "%REQ%"=="%OLD%" goto deps_ok

echo Устанавливаю компоненты, это может занять несколько минут...
venv\Scripts\python -m pip install -q -r requirements.txt
if errorlevel 1 goto pip_failed
>"%STAMP%" echo %REQ%
goto shortcuts

:deps_ok
echo Компоненты уже установлены.

:shortcuts
echo Создаю ярлыки...
venv\Scripts\python translator.py --setup
if errorlevel 1 echo [!] Ярлыки не созданы - подробности в log.txt. Программа всё равно запустится.

rem Запуск без окна консоли. Управление - иконка в трее, сообщения - в log.txt.
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0translator.py"
exit /b 0

:no_python
echo.
echo [!] Не найден Python. Установите Python 3.10 или новее с сайта python.org
echo     и при установке отметьте галочку "Add python.exe to PATH".
echo     Потом запустите start.bat ещё раз.
if exist venv rmdir /s /q venv
pause
exit /b 1

:pip_failed
echo.
echo [!] Не удалось установить компоненты - см. сообщения выше.
echo     Проверьте интернет и запустите start.bat ещё раз.
pause
exit /b 1
