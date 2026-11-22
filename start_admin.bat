@echo off
rem То же, что start.bat, но от имени администратора — для игр, которые сами
rem запускаются от админа (Genshin Impact и т.п.), иначе горячие клавиши не ловятся.
rem Удобнее включить в программе: Настройки -> «Запускать от имени администратора»,
rem тогда и ярлык на рабочем столе будет запускать её с нужными правами.
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
call "%~dp0start.bat"
