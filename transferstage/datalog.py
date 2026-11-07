"""Session data logger: one CSV per session in ``<logging.dir>/YYYYMMDD_HHMMSS.csv``.

Connect :meth:`DataLogger.on_thermal` to ``ThermalWorker.sample`` and
:meth:`DataLogger.on_motion` to ``MotionWorker.status``. The logger keeps the
latest of each and writes one row every ``interval_s`` (a QTimer in the
logger's thread, normally the GUI thread). Every row is flushed to disk.

Extra columns (appended, so older column positions are unchanged):

* ``sample_age_s``: seconds since the last thermal sample arrived. It grows if
  the thermal loop stalls, so repeated values can be told apart from a steady
  temperature.
* ``note``: free text from :meth:`DataLogger.mark`, which writes an extra row
  at the moment of the mark.

:meth:`DataLogger.open` can also write ``<stem>.meta.yaml`` next to the CSV
(start time, version, host, connection errors, the full configuration).
"""

from __future__ import annotations

import csv
import logging
import platform
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import yaml
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
    "sample_age_s",
    "note",
]


def run_metadata(config: Any = None, **extra: Any) -> dict[str, Any]:
    """Session metadata for the ``.meta.yaml`` sidecar: version, host, plus ``extra`` and the config."""
    from . import __version__

    meta: dict[str, Any] = {
        "started": datetime.now().isoformat(timespec="seconds"),
        "app_version": __version__,
        "host": platform.node(),
        "python": platform.python_version(),
    }
    meta.update(extra)
    if config is not None:
        meta["config"] = config.to_dict()
    return meta


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
        self.meta_path: Path | None = None
        self.rows_written = 0
        self.marks = 0
        self._thermal: ThermalSample | None = None
        self._thermal_at: float | None = None  # logger clock when the last thermal sample arrived
        self._motion: MotionStatus | None = None

    @classmethod
    def from_config(cls, cfg: LoggingConfig, **kw) -> DataLogger:
        return cls(cfg.dir, cfg.interval_s, **kw)

    @property
    def is_open(self) -> bool:
        return self._fh is not None

    # ------------------------------------------------------------------ lifecycle

    def start(self, metadata: dict[str, Any] | None = None) -> Path:
        """Create the session file, write the header and start the row timer."""
        path = self.open(metadata)
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self.write_row)
        self._timer.start(int(round(self.interval_s * 1000)))
        return path

    def open(self, metadata: dict[str, Any] | None = None) -> Path:
        """Create the session file and write the header, without a timer (call :meth:`write_row` yourself).

        ``metadata`` (e.g. from :func:`run_metadata`) is written to ``<stem>.meta.yaml``;
        a failure there is logged but does not stop the data log.
        """
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
            if metadata is not None:
                self._write_metadata(metadata)
        return self.path

    def _write_metadata(self, metadata: dict[str, Any]) -> None:
        meta_path = self.path.with_suffix(".meta.yaml")
        meta = {"data_file": self.path.name, **metadata}
        try:
            with meta_path.open("w", encoding="utf-8") as fh:
                yaml.safe_dump(meta, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)
            self.meta_path = meta_path
        except (OSError, yaml.YAMLError) as exc:
            log.error("data log: metadata not written: %s", exc)

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
        self._thermal_at = self._clock()

    @pyqtSlot(object)
    def on_motion(self, status: MotionStatus) -> None:
        self._motion = status

    # ------------------------------------------------------------------ output

    def _row(self, note: str = "") -> list[str]:
        th, mo = self._thermal, self._motion
        now = self._clock()
        row = [self._now().isoformat(timespec="milliseconds"), f"{now - self._t0:.3f}"]
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
        row.append("" if self._thermal_at is None else f"{now - self._thermal_at:.3f}")
        row.append(note)
        return row

    @pyqtSlot()
    def write_row(self) -> None:
        self._write(self._row())

    def _write(self, row: list[str]) -> bool:
        if self._writer is None or self._fh is None:
            return False
        try:
            self._writer.writerow(row)
            self._fh.flush()
            self.rows_written += 1
            return True
        except OSError as exc:
            log.error("data log: write failed: %s", exc)
            self.error.emit(f"data log write failed: {exc}")
            return False

    def mark(self, text: str = "") -> str:
        """Write an extra row now with ``text`` in the ``note`` column (empty text: ``mark N``).

        The note is also written to the application log. Returns the note.
        """
        self.marks += 1
        note = " ".join(str(text).split()) or f"mark {self.marks}"  # one line, no stray whitespace
        log.info("MARK: %s", note)
        if not self._write(self._row(note)):
            log.warning("data log: mark %r not written to the CSV (log not open)", note)
        return note
