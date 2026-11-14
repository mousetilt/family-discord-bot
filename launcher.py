# -*- coding: utf-8 -*-
"""Загрузчик для хостинга: при каждом старте подтягивает свежий код из GitHub и запускает bot.py.

На хосте ставится как стартовый файл (STARTUP_FILE=launcher.py). Только стандартная библиотека.
- есть git → репозиторий в этой же папке (при первом запуске создаётся на месте), код = origin/main;
- нет git → скачивает zip-архив репозитория и раскладывает поверх.
Файлы из .gitignore (база *.db, .env, логи, .local) НЕ трогаются. Любая ошибка обновления —
не повод не запускаться: бот стартует на текущем коде.
"""
import hashlib
import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

REPO = "https://github.com/mousetilt/family-discord-bot.git"
BRANCH = "main"
ZIP_URL = f"https://codeload.github.com/mousetilt/family-discord-bot/zip/refs/heads/{BRANCH}"
ROOT = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    line = f"[launcher] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:  # консоль не в UTF-8 — не падаем из-за журнала
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)


def git(*args, timeout=180):
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()[-400:]}")
    return p.stdout.strip()


def sync_git():
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        log("первый запуск: превращаю папку в git-репозиторий (база и .env остаются)…")
        git("init", "-q")
        git("remote", "add", "origin", REPO)
    git("fetch", "-q", "origin", BRANCH, timeout=300)
    old = ""
    try:
        old = git("rev-parse", "HEAD")
    except RuntimeError:
        pass
    new = git("rev-parse", f"origin/{BRANCH}")
    if old == new:
        log(f"код актуален ({new[:7]})")
        return
    # reset --hard меняет только отслеживаемые файлы; игнорируемые (.db, .env, .local) не трогает
    git("reset", "-q", "--hard", f"origin/{BRANCH}")
    log(f"код обновлён: {old[:7] or '—'} → {new[:7]}")


def sync_zip():
    log("git не найден — скачиваю архив репозитория…")
    with urllib.request.urlopen(ZIP_URL, timeout=120) as r:
        data = r.read()
    root_real = os.path.realpath(ROOT)
    n = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            parts = info.filename.split("/", 1)
            if len(parts) < 2 or not parts[1] or info.is_dir():
                continue
            dest = os.path.realpath(os.path.join(ROOT, parts[1]))
            if not dest.startswith(root_real + os.sep):  # защита от путей вида ../
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            n += 1
    log(f"архив разложен: {n} файлов")


def req_hash():
    try:
        with open(os.path.join(ROOT, "requirements.txt"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def main():
    before = req_hash()
    try:
        if shutil.which("git"):
            sync_git()
        else:
            sync_zip()
    except Exception as e:
        log(f"⚠️ обновиться не удалось, запускаюсь на текущем коде: {e}")
    if req_hash() != before:
        log("requirements.txt изменился — ставлю зависимости…")
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-U",
                            "--prefix", ".local", "-r", "requirements.txt"], cwd=ROOT, timeout=900)
        except Exception as e:
            log(f"⚠️ pip не отработал: {e}")
    log("запускаю бота…")
    os.chdir(ROOT)
    os.execv(sys.executable, [sys.executable, "-u", os.path.join(ROOT, "bot.py")])


if __name__ == "__main__":
    main()
