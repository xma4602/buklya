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

Формат строки — ровные столбцы: время | уровень | источник | сообщение.
Имя источника (логгера) всегда занимает NAME_WIDTH символов: длинные имена
сокращаются слева по пакетам (buklya.telegram.handlers.compose -> b.t.handlers.compose).
В консоли уровень, время и источник раскрашиваются (LOG_COLOR), в файле — без цвета.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
FILE_PREFIX = "buklya_"

#: Ширина столбца с источником сообщения (именем логгера)
NAME_WIDTH = 24
#: Ширина столбца с уровнем (самый длинный — CRITICAL)
LEVEL_WIDTH = 8

# ANSI-цвета для консоли
_RESET = "\033[0m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_LEVEL_COLORS = {
    logging.DEBUG: "\033[2;37m",     # серый
    logging.INFO: "\033[32m",        # зелёный
    logging.WARNING: "\033[33m",     # жёлтый
    logging.ERROR: "\033[31m",       # красный
    logging.CRITICAL: "\033[1;41;97m",  # белый на красном
}


def short_name(name: str, width: int = NAME_WIDTH) -> str:
    """
    Имя логгера ровно в `width` символов.

    Длинное имя сокращается как в logback: пакеты слева заменяются первой
    буквой, пока не влезет (buklya.vk.handlers.accounts -> b.vk.handlers.accounts);
    если и так не влезает — обрезается слева с «…».
    """
    parts = name.split(".")
    index = 0
    while len(".".join(parts)) > width and index < len(parts) - 1:
        parts[index] = parts[index][:1]
        index += 1
    result = ".".join(parts)
    if len(result) > width:
        result = "…" + result[-(width - 1):]
    return result.ljust(width)


class AlignedFormatter(logging.Formatter):
    """Ровные столбцы: время | уровень | источник | сообщение."""

    def __init__(self, color: bool = False) -> None:
        super().__init__(datefmt=DATE_FORMAT)
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        asctime = self.formatTime(record, self.datefmt)
        level = record.levelname.ljust(LEVEL_WIDTH)
        name = short_name(record.name)
        message = record.getMessage()
        if self.color:
            level_color = _LEVEL_COLORS.get(record.levelno, "")
            asctime = f"{_DIM}{asctime}{_RESET}"
            level = f"{level_color}{level}{_RESET}"
            name = f"{_CYAN}{name}{_RESET}"
            if record.levelno >= logging.WARNING:
                message = f"{level_color}{message}{_RESET}"
        line = f"{asctime} | {level} | {name} | {message}"
        # Трассировка исключения и стек — с новой строки, как у стандартного Formatter
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        extra = record.exc_text or ""
        if record.stack_info:
            extra = f"{extra}\n{self.formatStack(record.stack_info)}" if extra else self.formatStack(record.stack_info)
        if extra:
            line = f"{line}\n{_LEVEL_COLORS[logging.ERROR]}{extra}{_RESET}" if self.color else f"{line}\n{extra}"
        return line


def use_color(setting: str = "auto", stream=None) -> bool:
    """
    Включать ли цвет в консоли.

    :param setting: "true"/"false" — принудительно; "auto" — если консоль это поддерживает:
        настоящий терминал, консоль PyCharm, или задан FORCE_COLOR. NO_COLOR — всегда без цвета.
    """
    setting = (setting or "auto").strip().lower()
    if setting in ("1", "true", "yes", "on"):
        return True
    if setting in ("0", "false", "no", "off") or os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("PYCHARM_HOSTED"):
        return True
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())


def _enable_windows_ansi() -> None:
    """Старым консолям Windows нужно явно включить поддержку ANSI-цветов."""
    if os.name != "nt":
        return
    try:
        import colorama  # ставится вместе с vkbottle

        colorama.just_fix_windows_console()
    except Exception:  # noqa: BLE001 — без colorama Windows Terminal и PyCharm всё равно справятся
        os.system("")  # включает VT-режим в conhost


class DailySessionFileHandler(logging.FileHandler):
    """
    Файловый обработчик, который:
      * при создании открывает новый файл с меткой времени в имени;
      * при смене дня переключается на новый файл.
    """

    def __init__(
        self, log_dir: Path, retention_days: int = 30, encoding: str = "utf-8", prefix: str = FILE_PREFIX
    ) -> None:
        self.log_dir = Path(log_dir)
        #: Префикс имени файла. У раздельно запущенных ботов он свой (buklya-vk_…),
        #: чтобы контейнеры с общей папкой логов не писали в один файл.
        self.prefix = prefix
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._current_date: date = date.today()
        super().__init__(self._new_filename(), mode="a", encoding=encoding, delay=False)
        self._cleanup_old_files()

    # ------------------------------------------------------------------ helpers
    def _new_filename(self) -> str:
        """Формирует уникальное имя файла: префикс + дата + время запуска."""
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = self.log_dir / f"{self.prefix}{stamp}.log"
        # На случай двух перезапусков в одну секунду — добавляем суффикс
        counter = 1
        while path.exists():
            path = self.log_dir / f"{self.prefix}{stamp}_{counter}.log"
            counter += 1
        return str(path)

    def _cleanup_old_files(self) -> None:
        """Удаляет лог-файлы старше retention_days (если retention_days > 0)."""
        if self.retention_days <= 0:
            return
        border = time.time() - timedelta(days=self.retention_days).total_seconds()
        for file in self.log_dir.glob(f"{self.prefix}*.log"):
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


def setup_logging(
    log_dir: Path,
    level: str = "INFO",
    retention_days: int = 30,
    prefix: str = FILE_PREFIX,
    color: str = "auto",
) -> logging.Logger:
    """
    Настраивает корневой логгер: вывод в консоль + в файл (DailySessionFileHandler).

    :param prefix: начало имени лог-файла (например, «buklya-vk_» для отдельного ВК-бота)
    :param color: цвет в консоли: auto | true | false (в файл цвет не пишется никогда)

    Возвращает логгер приложения `buklya`.
    """
    colored = use_color(color)
    if colored:
        _enable_windows_ansi()

    root = logging.getLogger()
    root.setLevel(level)
    # Удаляем обработчики, если setup_logging вызывается повторно
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(AlignedFormatter(color=colored))
    root.addHandler(console)

    file_handler = DailySessionFileHandler(log_dir, retention_days=retention_days, prefix=prefix)
    file_handler.setFormatter(AlignedFormatter(color=False))
    root.addHandler(file_handler)

    # Сторонние библиотеки слишком «болтливы» на DEBUG/INFO
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("vkbottle").setLevel(logging.WARNING if level != "DEBUG" else logging.INFO)

    logger = logging.getLogger("buklya")
    logger.info("Логирование запущено, файл: %s", file_handler.baseFilename)
    return logger
