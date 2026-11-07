"""Session data logger: one CSV per session in ``<logging.dir>/YYYYMMDD_HHMMSS.csv``.

Connect :meth:`DataLogger.on_thermal` to ``ThermalWorker.sample`` and
:meth:`DataLogger.on_motion` to ``MotionWorker.status``. The logger keeps the
latest of each and writes one row every ``interval_s`` (a QTimer in the
logger's thread, normally the GUI thread). Every row is flushed to disk.
"""

from __future__ import annotations

import csv
import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import IO

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from .config import PROJECT_ROOT, LoggingConfig
from .control.motion import MotionStatus
from .control.thermal import ThermalSample

log = logging.getLogger(__name__)

COLUMNS = [
    "timestamp",
    "elapsed_s",
    "temperature_c",
    "target_sp_c",
    "program_sp_c",
    "v_cmd",
    "v_read",
    "i_read",
    "psu_on",
    "ramping",
    "profile_segment",
    "profile_phase",
    "mode",
    "z_um",
    "z_state",
    "speed_um_s",
    "rotator_deg",
    "rotator_state",
    "safe_lock",
    "alarms",
]


def _num(x: float | None, digits: int) -> str:
    return "" if x is None else f"{x:.{digits}f}"


class DataLogger(QObject):
    error = pyqtSignal(str)

    def __init__(
        self,
        directory: str | Path = "logs",
        interval_s: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        d = Path(directory)
        self.directory = d if d.is_absolute() else PROJECT_ROOT / d
        if not interval_s > 0:
            raise ValueError("interval_s must be > 0")
        self.interval_s = float(interval_s)
        self._clock = clock
        self._now = now
        self._fh: IO[str] | None = None
        self._writer = None
        self._timer: QTimer | None = None
        self._t0 = 0.0
        self.path: Path | None = None
        self.rows_written = 0
        self._thermal: ThermalSample | None = None
        self._motion: MotionStatus | None = None

    @classmethod
    def from_config(cls, cfg: LoggingConfig, **kw) -> DataLogger:
        return cls(cfg.dir, cfg.interval_s, **kw)

    @property
    def is_open(self) -> bool:
        return self._fh is not None

    # ------------------------------------------------------------------ lifecycle

    @pyqtSlot()
    def start(self) -> Path:
        """Create the session file, write the header and start the row timer."""
        path = self.open()
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self.write_row)
        self._timer.start(int(round(self.interval_s * 1000)))
        return path

    def open(self) -> Path:
        """Create the session file and write the header, without a timer (call :meth:`write_row` yourself)."""
        if self._fh is None:
            self.directory.mkdir(parents=True, exist_ok=True)
            stem = self._now().strftime("%Y%m%d_%H%M%S")
            path = self.directory / f"{stem}.csv"
            n = 1
            while path.exists():
                path = self.directory / f"{stem}_{n}.csv"
                n += 1
            self._fh = path.open("x", newline="", encoding="utf-8")
            self._writer = csv.writer(self._fh)
            self._writer.writerow(COLUMNS)
            self._fh.flush()
            self.path = path
            self._t0 = self._clock()
            log.info("data log: %s", path)
        return self.path

    @pyqtSlot()
    def stop(self) -> None:
        self.close()

    @pyqtSlot()
    def close(self) -> None:
        """Stop the timer and close the file. Idempotent."""
        if self._timer is not None:
            self._timer.stop()
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError as exc:
                log.error("data log: close failed: %s", exc)
            self._fh = None
            self._writer = None

    # ------------------------------------------------------------------ inputs

    @pyqtSlot(object)
    def on_thermal(self, sample: ThermalSample) -> None:
        self._thermal = sample

    @pyqtSlot(object)
    def on_motion(self, status: MotionStatus) -> None:
        self._motion = status

    # ------------------------------------------------------------------ output

    def _row(self) -> list[str]:
        th, mo = self._thermal, self._motion
        row = [self._now().isoformat(timespec="milliseconds"), f"{self._clock() - self._t0:.3f}"]
        if th is None:
            row += [""] * 11
        else:
            row += [
                _num(th.temperature_c, 3),
                _num(th.target_sp_c, 2),
                _num(th.program_sp_c, 3),
                _num(th.v_cmd, 3),
                _num(th.v_read, 3),
                _num(th.i_read, 3),
                str(int(th.psu_on)),
                str(int(th.ramping)),
                "" if th.profile_segment is None else str(th.profile_segment),
                th.profile_phase or "",
                th.mode,
            ]
        if mo is None:
            row += [""] * 6
        else:
            row += [
                _num(mo.z_um, 2),
                mo.z_state.value,
                _num(mo.speed_um_s, 1),
                _num(mo.rotator_deg, 3),
                mo.rotator_state.value,
                str(int(mo.safe_lock)),
            ]
        row.append("" if th is None else ";".join(a.code.value for a in th.alarms))
        return row

    @pyqtSlot()
    def write_row(self) -> None:
        if self._writer is None or self._fh is None:
            return
        try:
            self._writer.writerow(self._row())
            self._fh.flush()
            self.rows_written += 1
        except OSError as exc:
            log.error("data log: write failed: %s", exc)
            self.error.emit(f"data log write failed: {exc}")
