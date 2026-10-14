"""Логирование: новый файл при запуске и при смене дня."""

import logging
from datetime import date, timedelta

from buklya.logging_setup import DailySessionFileHandler


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
