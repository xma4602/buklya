"""Логирование: новый файл при запуске и при смене дня."""

import logging
from datetime import date, timedelta

from buklya.logging_setup import NAME_WIDTH, AlignedFormatter, DailySessionFileHandler, short_name


def _make(tmp_path):
    h = DailySessionFileHandler(tmp_path, retention_days=0)
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


def test_new_file_per_start(tmp_path):
    h1 = _make(tmp_path)
    h2 = _make(tmp_path)  # «перезапуск» в ту же секунду
    assert h1.baseFilename != h2.baseFilename
    h1.close()
    h2.close()
    assert len(list(tmp_path.glob("buklya_*.log"))) == 2


def test_rollover_on_new_day(tmp_path):
    h = _make(tmp_path)
    first = h.baseFilename
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    h.emit(rec)
    h._current_date = date.today() - timedelta(days=1)  # имитируем полночь
    h.emit(rec)
    assert h.baseFilename != first
    h.close()
    assert len(list(tmp_path.glob("buklya_*.log"))) == 2


def test_columns_are_aligned():
    """Столбцы ровные при любой длине имени логгера и уровня; в файле нет ANSI-цветов."""
    fmt = AlignedFormatter(color=False)
    names = ["buklya", "buklya.vk.handlers.accounts", "buklya.telegram.handlers.compose",
             "vkbottle.exception_factory.error_handler.error_handler"]
    lines = []
    for name, level in zip(names, [logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL]):
        lines.append(fmt.format(logging.LogRecord(name, level, __file__, 1, "msg", None, None)))
    positions = {line.index("| msg") for line in lines}
    assert len(positions) == 1, lines
    assert all("\033[" not in line for line in lines)
    assert short_name("buklya.telegram.handlers.compose") == "b.t.handlers.compose".ljust(NAME_WIDTH)


def test_color_formatter_marks_levels():
    line = AlignedFormatter(color=True).format(logging.LogRecord("buklya", logging.ERROR, __file__, 1, "x", None, None))
    assert "\033[31m" in line
