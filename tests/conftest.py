"""Общие настройки тестов."""

import os
import sys
from pathlib import Path

# Чтобы `import buklya` работал без установки пакета
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Минимальное окружение для Settings
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("ENCRYPTION_KEY", "q2c7Q3f1lYp8m0p8w0Z9b3m6m3c2s7m1V2Y4Q0b9c1E=")
