"""Main window: Manual / Temperature / Settings / Log tabs, status bar, shortcuts, shutdown."""

from __future__ import annotations

import logging

from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import QLabel, QMainWindow, QMessageBox, QTabWidget

from ..app import DEVICE_NAMES, AppController
from ..config import PROJECT_ROOT
from .input_map import InputMap
from .log_tab import LogTab
from .manual_tab import ManualTab
from .settings_tab import SettingsTab
from .temperature_tab import TemperatureTab
from .widgets import AMBER, GREEN, RED, Led

log = logging.getLogger(__name__)

DEVICE_LABELS = {"z": "Z", "rotator": "Rotator", "psu": "PSU", "thermometer": "DMM"}


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController, *, confirm_close: bool = True) -> None:
        super().__init__()
        self.c = controller
        self.confirm_close = confirm_close
        self.setWindowTitle("Transfer Stage Control")
        self.resize(1400, 860)

        self.tabs = QTabWidget()
        self.manual = ManualTab(controller)
        self.temperature = TemperatureTab(controller)
        self.settings = SettingsTab(controller)
        log_dir = controller.datalog.directory if controller.datalog is not None else PROJECT_ROOT / "logs"
        self.log_tab = LogTab(log_dir)
        self.tabs.addTab(self.manual, "Manual")
        self.tabs.addTab(self.temperature, "Temperature")
        self.tabs.addTab(self.settings, "Settings")
        self.tabs.addTab(self.log_tab, "Log")
        self.setCentralWidget(self.tabs)

        self._build_status_bar()

        # worker -> GUI
        controller.motion.status.connect(self.manual.on_status)
        controller.thermal.sample.connect(self.temperature.on_sample)
        controller.thermal.autotune_progress.connect(self.temperature.on_autotune_progress)
        controller.thermal.autotune_finished.connect(self.temperature.on_autotune_finished)
        controller.thermal.sample.connect(self._on_sample)
        controller.thermal.alarm.connect(self.log_tab.add_alarm)
        for sig in (controller.motion.error, controller.thermal.error):
            sig.connect(lambda m: self._notify(m, error=True))
        for sig in (controller.motion.refused, controller.thermal.refused):
            sig.connect(lambda m: self._notify(m))
        for tab in (self.manual, self.temperature, self.settings):
            tab.message.connect(self._notify)
        self.settings.saved.connect(self._on_settings_saved)

        # TourBox / keyboard
        self.input_map = InputMap(controller.config.keys, self)
        self.input_map.jog_start.connect(controller.jog_start.emit)
        self.input_map.jog_stop.connect(controller.jog_stop.emit)
        self.input_map.action.connect(self._on_action)
        self.input_map.install()

        self._conn_timer = QTimer(self)
        self._conn_timer.timeout.connect(self._refresh_connections)
        self._conn_timer.start(1000)
        self._refresh_connections()

    # ------------------------------------------------------------------ status bar

    def _build_status_bar(self) -> None:
        sb = self.statusBar()
        self.conn_leds: dict[str, Led] = {}
        for name in DEVICE_NAMES:
            led = Led(DEVICE_LABELS[name], GREEN)
            self.conn_leds[name] = led
            sb.addPermanentWidget(led)
        self.sim_label = QLabel(self._sim_text())
        sb.addPermanentWidget(self.sim_label)
        self.alarm_led = Led("No alarm", RED)
        sb.addPermanentWidget(self.alarm_led)

    def _sim_text(self) -> str:
        s = self.c.config.devices.simulate
        names = [DEVICE_LABELS[n] for n, flag in zip(DEVICE_NAMES, (s.z, s.rotator, s.psu, s.dmm)) if flag]
        return f"  [SIM: {', '.join(names)}]" if names else ""

    def _refresh_connections(self) -> None:
        for name, led in self.conn_leds.items():
            ok = self.c.device_connected(name)
            led.set_on(True, GREEN if ok else RED)
            led.setToolTip(
                "connected" if ok else f"not connected{': ' + str(self.c.connect_errors[name]) if name in self.c.connect_errors else ''}"
            )

    def _on_sample(self, s) -> None:
        active = [a for a in s.alarms if a.active or a.is_trip]
        if active:
            trip = any(a.is_trip for a in active)
            self.alarm_led.set_text(("TRIP: " if trip else "WARNING: ") + ", ".join(a.code.value for a in active))
            self.alarm_led.set_on(True, RED if trip else AMBER)
        elif self.alarm_led.is_on:
            self.alarm_led.set_text("No alarm")
            self.alarm_led.set_on(False)

    def _notify(self, msg: str, error: bool = False) -> None:
        self.statusBar().showMessage(("⚠ " if error else "") + msg, 8000)

    # ------------------------------------------------------------------ actions

    def _on_action(self, action: str) -> None:
        if action == "safe_lock":
            self.manual.toggle_safe_lock()
        elif action == "cancel_motion":
            self.c.cancel_motion.emit()
        elif action == "speed_up":
            self.manual.nudge_speed(+1)
        elif action == "speed_down":
            self.manual.nudge_speed(-1)

    def _on_settings_saved(self, cfg) -> None:
        self.input_map.set_bindings(cfg.keys)
        self.c.config.keys = dict(cfg.keys)

    # ------------------------------------------------------------------ shutdown

    def closeEvent(self, ev: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self.confirm_close and self.c.psu_on:
            r = QMessageBox.question(
                self,
                "Quit",
                "The PSU output is ON. Quitting switches the heater off and stops all motion.\n\nQuit anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                ev.ignore()
                return
        self._conn_timer.stop()
        self.input_map.uninstall()
        self.c.shutdown()
        self.log_tab.detach()
        ev.accept()
