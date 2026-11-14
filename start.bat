@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Бот семьи Ульяновы

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  python --version >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python не найден. Пробую установить Python 3.12...
  winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
  echo.
  echo Если установка прошла — закрой окно и запусти start.bat ещё раз.
  echo Если нет — скачай Python с python.org и поставь галочку "Add python.exe to PATH".
  pause
  exit /b
)

if not exist ".venv\Scripts\python.exe" (
  echo Создаю окружение...
  %PY% -m venv .venv
)

fc /b requirements.txt .venv\req.txt >nul 2>nul
if errorlevel 1 (
  echo Устанавливаю или обновляю зависимости...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if not errorlevel 1 copy /y requirements.txt .venv\req.txt >nul
)

if not exist ".env" (
  copy .env.example .env >nul
  echo Открой .env, вставь токен бота и ID сервера, сохрани и запусти start.bat снова.
  notepad .env
  pause
  exit /b
)

:run
".venv\Scripts\python.exe" bot.py
if errorlevel 2 (
  echo.
  echo Исправь ошибку выше и запусти start.bat снова.
  pause
  exit /b
)
echo.
echo Бот остановился. Перезапуск через 10 секунд — закрой окно, чтобы выйти.
timeout /t 10 >nul
goto run
