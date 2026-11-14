#!/bin/bash
# Стартовый скрипт для хостинга (bot-hosting / pterodactyl).
# Подтягивает свежий код из GitHub, ставит зависимости и запускает бота.
# Токен для git берётся из .git/config на хосте (НЕ из этого файла) — секретов тут нет.
cd "$(dirname "$0")" || exit 1

echo "⬇️  git pull…"
git pull --ff-only || echo "⚠️  git pull не удался — запускаюсь на текущем коде."

echo "📦  зависимости…"
pip install --disable-pip-version-check -U --prefix .local -r requirements.txt

echo "🚀  запуск бота…"
exec python3 -u bot.py
