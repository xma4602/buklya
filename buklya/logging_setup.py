"""
Настройка логирования.

Требования:
    * каждый день — новый лог-файл;
    * при каждом перезапуске — тоже новый файл (даже если день тот же).

Реализация: собственный обработчик `DailySessionFileHandler`. Имя файла
содержит дату и время открытия, например `buklya_2026-10-14_12-59-03.log`.
При старте приложения открывается новый файл; при смене календарной даты
текущий файл закрывается и открывается следующий с новой датой.
Старые файлы старше `retention_days` удаляются.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
FILE_PREFIX = "buklya_"


class DailySessionFileHandler(logging.FileHandler):
    """
    Файловый обработчик, который:
      * при создании открывает новый файл с меткой времени в имени;
      * при смене дня переключается на новый файл.
    """

    def __init__(self, log_dir: Path, retention_days: int = 30, encoding: str = "utf-8") -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._current_date: date = date.today()
        super().__init__(self._new_filename(), mode="a", encoding=encoding, delay=False)
        self._cleanup_old_files()

    # ------------------------------------------------------------------ helpers
    def _new_filename(self) -> str:
        """Формирует уникальное имя файла: префикс + дата + время запуска."""
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = self.log_dir / f"{FILE_PREFIX}{stamp}.log"
        # На случай двух перезапусков в одну секунду — добавляем суффикс
        counter = 1
        while path.exists():
            path = self.log_dir / f"{FILE_PREFIX}{stamp}_{counter}.log"
            counter += 1
        return str(path)

    def _cleanup_old_files(self) -> None:
        """Удаляет лог-файлы старше retention_days (если retention_days > 0)."""
        if self.retention_days <= 0:
            return
        border = time.time() - timedelta(days=self.retention_days).total_seconds()
        for file in self.log_dir.glob(f"{FILE_PREFIX}*.log"):
            try:
                if file.stat().st_mtime < border and str(file) != self.baseFilename:
                    file.unlink()
            except OSError:
                pass

    def _rollover(self) -> None:
        """Закрывает текущий файл и открывает новый (смена суток)."""
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        self.baseFilename = str(Path(self._new_filename()).resolve())
        self._current_date = date.today()
        self.stream = self._open()
        self._cleanup_old_files()

    # ------------------------------------------------------------------ API
    def emit(self, record: logging.LogRecord) -> None:
        """Перед записью проверяем, не наступил ли новый день."""
        try:
            if date.today() != self._current_date:
                self._rollover()
        except Exception:  # noqa: BLE001 — логгер не должен ронять приложение
            self.handleError(record)
            return
        super().emit(record)


def setup_logging(log_dir: Path, level: str = "INFO", retention_days: int = 30) -> logging.Logger:
    """
    Настраивает корневой логгер: вывод в консоль + в файл (DailySessionFileHandler).

    Возвращает логгер приложения `buklya`.
    """
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)
    # Удаляем обработчики, если setup_logging вызывается повторно
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = DailySessionFileHandler(log_dir, retention_days=retention_days)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Сторонние библиотеки слишком «болтливы» на DEBUG/INFO
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)

    logger = logging.getLogger("buklya")
    logger.info("Логирование запущено, файл: %s", file_handler.baseFilename)
    return logger
