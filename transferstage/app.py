"""Application entry point: ``python -m transferstage`` (or the ``transferstage`` script).

:class:`AppController` owns the configuration, the devices, the two worker
threads and the data logger. The GUI talks to the workers only through the
controller's signals, which are connected to worker slots and therefore queued
into the worker threads.
"""

from __future__ import annotations

import argparse
import copy
import logging
import sys
import traceback
from pathlib import Path

from PyQt5.QtCore import QObject, QThread, pyqtSignal
from PyQt5.QtWidgets import QApplication, QMessageBox

from .applog import setup_logging
from .config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config, save_config
from .control.motion import MotionWorker
from .control.safety import SafetyMonitor, safe_shutdown
from .control.thermal import ThermalWorker
from .control.threads import start_worker, stop_worker
from .datalog import DataLogger
from .drivers import Devices, build_devices

log = logging.getLogger(__name__)

DEVICE_NAMES = ("z", "rotator", "psu", "thermometer")


class AppController(QObject):
    # motion commands -> MotionWorker slots
    jog_start = pyqtSignal(int)
    jog_stop = pyqtSignal()
    move_z_to = pyqtSignal(float)
    set_speed = pyqtSignal(float)
    cancel_motion = pyqtSignal()
    rotate_to = pyqtSignal(float)
    home_z = pyqtSignal()
    home_rotator = pyqtSignal()
    set_safe_lock = pyqtSignal(bool)
    # thermal commands -> ThermalWorker slots
    set_target = pyqtSignal(float, float)
    set_psu_enabled = pyqtSignal(bool)
    set_gains = pyqtSignal(float, float, float)
    reset_pid = pyqtSignal()
    start_profile = pyqtSignal(object)
    stop_profile = pyqtSignal()
    acknowledge_alarm = pyqtSignal()
    start_autotune = pyqtSignal(object)  # AutotuneParams
    cancel_autotune = pyqtSignal()

    def __init__(
        self,
        config: Config,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        *,
        devices: Devices | None = None,
        sim_time_scale: float = 1.0,
        enable_datalog: bool = True,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config  # live configuration used by this run
        self.config_path = Path(config_path)
        self.saved_config = copy.deepcopy(config)  # what is on disk; Settings/presets edit this
        self.devices = devices if devices is not None else build_devices(config, time_scale=sim_time_scale)
        self.connect_errors: dict[str, Exception] = {}
        self.safety = SafetyMonitor(config.limits)
        self.motion = MotionWorker(self.devices.z, self.devices.rotator, self.safety)
        self.thermal = ThermalWorker(self.devices.psu, self.devices.thermometer, config)
        self.datalog = DataLogger.from_config(config.logging) if enable_datalog else None
        self._threads: list[tuple[QObject, QThread]] = []
        self._shut_down = False
        self.last_thermal = None  # latest ThermalSample (GUI thread)
        self.thermal.sample.connect(self._on_thermal)

        m, t = self.motion, self.thermal
        for sig, slot in (
            (self.jog_start, m.jog_start),
            (self.jog_stop, m.jog_stop),
            (self.move_z_to, m.move_z_to),
            (self.set_speed, m.set_speed),
            (self.cancel_motion, m.cancel_motion),
            (self.rotate_to, m.rotate_to),
            (self.home_z, m.home_z),
            (self.home_rotator, m.home_rotator),
            (self.set_safe_lock, m.set_safe_lock),
            (self.set_target, t.set_target),
            (self.set_psu_enabled, t.set_psu_enabled),
            (self.set_gains, t.set_gains),
            (self.reset_pid, t.reset_pid),
            (self.start_profile, t.start_profile),
            (self.stop_profile, t.stop_profile),
            (self.acknowledge_alarm, t.acknowledge_alarm),
            (self.start_autotune, t.start_autotune),
            (self.cancel_autotune, t.cancel_autotune),
        ):
            sig.connect(slot)
        if self.datalog is not None:
            self.thermal.sample.connect(self.datalog.on_thermal)
            self.motion.status.connect(self.datalog.on_motion)

    def _on_thermal(self, sample) -> None:
        self.last_thermal = sample

    @property
    def psu_on(self) -> bool:
        s = self.last_thermal
        return bool(s is not None and s.psu_on)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> dict[str, Exception]:
        """Connect the devices (= LabVIEW "Initialization") and start the workers and logger."""
        self.connect_errors = self.devices.connect_all()
        self._threads.append((self.motion, start_worker(self.motion, "motion")))
        self._threads.append((self.thermal, start_worker(self.thermal, "thermal")))
        if self.datalog is not None:
            try:
                self.datalog.start()
            except OSError as exc:
                log.error("data log could not be started: %s", exc)
        return self.connect_errors

    def device_connected(self, name: str) -> bool:
        try:
            return bool(getattr(self.devices, name).is_connected)
        except Exception:  # noqa: BLE001
            return False

    def shutdown(self) -> None:
        """Stop workers, close the data log, then ``safe_shutdown`` the devices. Idempotent."""
        if self._shut_down:
            return
        self._shut_down = True
        log.info("shutting down")
        for worker, thread in reversed(self._threads):
            try:
                if not stop_worker(worker, thread):
                    log.error("worker thread %s did not stop in time", thread.objectName())
            except Exception:  # noqa: BLE001
                log.exception("stopping worker failed")
        self._threads.clear()
        if self.datalog is not None:
            self.datalog.close()
        failures = safe_shutdown(self.devices)
        if failures:
            log.error("shutdown: %d step(s) failed: %s", len(failures), [n for n, _ in failures])

    # ------------------------------------------------------------------ config persistence

    def save_config(self, cfg: Config) -> Path:
        """Write ``cfg`` to the config file and remember it as the on-disk config."""
        path = save_config(cfg, self.config_path)
        self.saved_config = copy.deepcopy(cfg)
        log.info("configuration saved to %s", path)
        return path

    def save_presets(self, presets: list[dict]) -> None:
        cfg = copy.deepcopy(self.saved_config)
        cfg.presets = [dict(p) for p in presets]
        cfg.__post_init__()  # re-validate presets
        self.save_config(cfg)
        self.config.presets = copy.deepcopy(cfg.presets)


# --------------------------------------------------------------------------- exception hook


def install_excepthook(controller: AppController, app: QApplication) -> None:
    """Unhandled exceptions: log, make the hardware safe, tell the user, quit."""
    previous = sys.excepthook
    busy = False

    def hook(exc_type, exc, tb):
        nonlocal busy
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc, tb)
            return
        log.critical("unhandled exception:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        if busy:
            return
        busy = True
        try:
            controller.shutdown()
        finally:
            try:
                QMessageBox.critical(
                    None,
                    "TransferStageControl — fatal error",
                    f"An unexpected error occurred and the instruments were shut down safely:\n\n{exc_type.__name__}: {exc}"
                    "\n\nDetails are in logs/app.log. The application will close.",
                )
            except Exception:  # noqa: BLE001
                pass
            app.exit(1)

    sys.excepthook = hook


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="transferstage", description="Transfer stage control GUI")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="config file (default: config.yaml)")
    parser.add_argument(
        "--sim-time-scale", type=float, default=1.0, help="speed-up factor for the simulated thermal plant (demo only)"
    )
    parser.add_argument("--no-datalog", action="store_true", help="do not write a session CSV")
    args = parser.parse_args(argv)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("TransferStageControl")

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        setup_logging()
        log.error("config error: %s", exc)
        QMessageBox.critical(None, "TransferStageControl — config error", str(exc))
        return 2
    setup_logging(config.logging.dir)

    from .ui.main_window import MainWindow  # import after QApplication exists

    try:
        controller = AppController(
            config, args.config, sim_time_scale=args.sim_time_scale, enable_datalog=not args.no_datalog
        )
    except Exception as exc:  # noqa: BLE001 - e.g. Kinesis not installed
        log.exception("could not create devices")
        QMessageBox.critical(None, "TransferStageControl — device error", f"Could not create the devices:\n\n{exc}")
        return 3
    window = MainWindow(controller)
    install_excepthook(controller, app)
    errors = controller.start()
    window.show()
    if errors:
        QMessageBox.warning(
            window,
            "Device connection",
            "Some devices could not be connected:\n\n" + "\n".join(f"{k}: {v}" for k, v in errors.items()),
        )
    rc = app.exec_()
    controller.shutdown()
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
