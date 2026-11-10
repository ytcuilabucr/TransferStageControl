"""Main window, laid out like the LabVIEW front panel (gui_v2.vi).

    [Stop] [x Idle heater watchdog]                         Note: [...] [Mark]
    Z-Controller        | TourBox GUI | Kinesis GUI | Settings | ... |   PV/SP legend
    Temperature Control |   (centre tab pages)                      |   temperature chart
    Sample Rotator      |   TourBox shortcut legend                 |   voltage chart

Status bar: device connection LEDs, simulated devices, alarm state.
"""

from __future__ import annotations

import logging

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QCloseEvent, QColor, QIcon, QKeySequence, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..app import DEVICE_NAMES, AppController
from ..config import PROJECT_ROOT
from .idle_watchdog import IdleAlarmDialog, IdleWatchdog
from .input_map import InputMap
from .log_tab import LogTab
from .manual_tab import ManualPanel
from .settings_tab import SettingsTab
from .temperature_tab import TemperaturePanel
from .widgets import AMBER, GREEN, PANEL_STYLE, RED, Led

log = logging.getLogger(__name__)

DEVICE_LABELS = {"z": "Z", "rotator": "Rotator", "psu": "PSU", "thermometer": "DMM"}

# TourBox control -> action, in the order of the LabVIEW legend
LEGEND = (
    ("[Side] + [Top]", "Safe Lock", "safe_lock"),
    ("[Top]", "Cancel Motion", "cancel_motion"),
    ("[Knob]", "Speed Knob", None),
    ("[Tall]", "Moving Up", "jog_up"),
    ("[Short]", "Moving Down", "jog_down"),
)


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController, *, confirm_close: bool = True) -> None:
        super().__init__()
        self.c = controller
        self.confirm_close = confirm_close
        self.setWindowTitle("Transfer Stage Control")
        self.setStyleSheet(PANEL_STYLE)
        self.resize(1500, 880)

        self.manual = ManualPanel(controller, self)
        self.temperature = TemperaturePanel(controller, self)
        self.settings = SettingsTab(controller)
        log_dir = controller.datalog.directory if controller.datalog is not None else PROJECT_ROOT / "logs"
        self.log_tab = LogTab(log_dir)

        central = QWidget()
        central.setObjectName("panel")
        root = QVBoxLayout(central)
        root.addWidget(self.temperature.alarm_banner)

        top = QHBoxLayout()
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setToolTip("Stop all motion and switch the PSU output off")
        self.stop_btn.setMinimumSize(110, 36)
        self.stop_btn.setStyleSheet(
            "QPushButton { font-weight:bold; font-size:11pt; border:1px solid #888; border-radius:3px;"
            " background:#f0f0f0; padding:4px 12px; }"
            "QPushButton:hover { background:#fafafa; }"
        )
        red_square = QPixmap(16, 16)
        red_square.fill(QColor(RED))
        self.stop_btn.setIcon(QIcon(red_square))
        self.stop_btn.clicked.connect(self.stop_all)
        top.addWidget(self.stop_btn)
        top.addSpacing(16)
        lim = controller.config.limits
        self.watchdog_chk = QCheckBox()
        self.watchdog_chk.setChecked(True)
        self.watchdog_chk.clicked.connect(self._on_watchdog_clicked)
        top.addWidget(self.watchdog_chk)
        top.addStretch(1)
        top.addWidget(QLabel("Note:"))
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("e.g. stamp contact (empty = numbered mark)")
        self.note_edit.setMinimumWidth(280)
        self.note_edit.returnPressed.connect(self.add_mark)
        top.addWidget(self.note_edit)
        self.mark_btn = QPushButton("Mark")
        self.mark_btn.setToolTip("Write the note (or a numbered mark) to the session CSV and the log")
        self.mark_btn.clicked.connect(self.add_mark)
        top.addWidget(self.mark_btn)
        root.addLayout(top)

        body = QHBoxLayout()
        # left column
        left = QVBoxLayout()
        left.addWidget(self.manual.z_panel, 3)
        left.addSpacing(10)
        left.addWidget(self.temperature.control_panel)
        left.addSpacing(10)
        left.addWidget(self.manual.rotator_panel)
        left.addStretch(1)
        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setFixedWidth(360)
        body.addWidget(left_w)

        # centre: tabs + shortcut legend
        centre = QVBoxLayout()
        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("QTabWidget::pane { background:white; border:1px solid #a0a0a0; }")
        self.tabs.addTab(self.manual.tourbox_page, "TourBox GUI")
        self.tabs.addTab(self.manual.kinesis_page, "Kinesis GUI")
        self.tabs.addTab(self.temperature.settings_page, "Settings")
        self.tabs.addTab(self.temperature.profile_page, "Profile")
        self.tabs.addTab(self.temperature.autotune_page, "Autotune")
        self.tabs.addTab(self.settings, "Config")
        self.tabs.addTab(self.log_tab, "Log")
        centre.addWidget(self.tabs, 1)
        self.legend_label = QLabel()
        self.legend_label.setTextFormat(Qt.RichText)
        centre.addWidget(self.legend_label, alignment=Qt.AlignHCenter)
        centre_w = QWidget()
        centre_w.setLayout(centre)
        centre_w.setMinimumWidth(560)
        body.addWidget(centre_w, 3)

        # right: charts
        body.addWidget(self.temperature.charts, 4)
        root.addLayout(body, 1)
        self.setCentralWidget(central)
        self._update_legend(controller.config.keys)

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
        if controller.datalog is not None:
            controller.datalog.error.connect(lambda m: self._notify(m, error=True))

        # TourBox / keyboard
        self.input_map = InputMap(controller.config.keys, self)
        self.input_map.jog_start.connect(controller.jog_start.emit)
        self.input_map.jog_stop.connect(controller.jog_stop.emit)
        self.input_map.jog_start.connect(self.manual.show_key_jog)  # ▲/▼ look pressed while Tall/Short is held
        self.input_map.jog_stop.connect(lambda: self.manual.show_key_jog(0))
        self.input_map.action.connect(self._on_action)
        self.input_map.install()

        # idle-heater watchdog (on at every start; turning it off needs confirmation)
        self.watchdog = IdleWatchdog(
            lambda: self.c.psu_on, timeout_s=lim.idle_timeout_min * 60, grace_s=lim.idle_grace_min * 60, parent=self
        )
        self.idle_dialog = IdleAlarmDialog(self)
        self.watchdog.warning_started.connect(self._on_idle_warning)
        self.watchdog.countdown.connect(self.idle_dialog.set_remaining)
        self.watchdog.warning_cleared.connect(lambda _reason: self.idle_dialog.hide())
        self.watchdog.heater_off.connect(self._on_idle_shutoff)
        self.idle_dialog.answered.connect(self.watchdog.acknowledge)
        self.idle_dialog.off_requested.connect(self._on_idle_off_requested)
        self._update_watchdog_text()
        self.watchdog.start()

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
        elif action == "mark":
            self.add_mark()

    def add_mark(self) -> None:
        """Mark button / Enter in the note field / mark shortcut: event note in the CSV and the log."""
        note = self.c.mark(self.note_edit.text())
        self.note_edit.clear()
        self._notify(f"Marked: {note}" if note is not None else "Marked in the log (data log is off)")

    # ------------------------------------------------------------------ idle watchdog

    def _update_watchdog_text(self) -> None:
        w = self.watchdog
        self.watchdog_chk.setText(f"Idle heater watchdog ({w.timeout_s / 60:g} + {w.grace_s / 60:g} min)")
        self.watchdog_chk.setToolTip(
            f"Heater on and no keyboard/mouse input on this PC for {w.timeout_s / 60:g} min -> alarm;\n"
            f"alarm not answered within {w.grace_s / 60:g} min -> heater off."
        )
        self.watchdog_chk.setStyleSheet("" if w.enabled else f"QCheckBox {{ color:{RED}; font-weight:bold; }}")

    def _confirm_disable_watchdog(self) -> bool:
        r = QMessageBox.warning(
            self,
            "Idle heater watchdog",
            "Turn the idle heater watchdog OFF?\n\n"
            "The heater will then stay on even if nobody is at the PC. "
            "It is switched on again at the next program start.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return r == QMessageBox.Yes

    def _on_watchdog_clicked(self, checked: bool) -> None:
        if not checked and not self._confirm_disable_watchdog():
            self.watchdog_chk.setChecked(True)
            return
        self.watchdog.set_enabled(checked)
        self._update_watchdog_text()
        self.c.mark(f"idle watchdog {'on' if checked else 'OFF'}")
        self._notify("Idle heater watchdog on" if checked else "Idle heater watchdog OFF", error=not checked)

    def _on_idle_warning(self, grace_s: float) -> None:
        self.c.mark("idle alarm: heater on, no input")
        self.idle_dialog.show_warning(self.watchdog.timeout_s / 60, grace_s)

    def _on_idle_shutoff(self) -> None:
        self.c.set_psu_enabled.emit(False)
        if self.idle_dialog.ok_btn.isHidden():  # first time, not a retry
            self.c.mark("idle alarm not answered: heater off")
            self.idle_dialog.show_shut_off()
            self._notify("Idle alarm not answered: PSU output switched off", error=True)

    def _on_idle_off_requested(self) -> None:
        self.c.set_psu_enabled.emit(False)
        self.c.mark("idle alarm: heater off by user")
        self._notify("PSU output off")

    def _on_settings_saved(self, cfg) -> None:
        self.input_map.set_bindings(cfg.keys)
        self.c.config.keys = dict(cfg.keys)
        self._update_legend(cfg.keys)
        self.watchdog.set_times(cfg.limits.idle_timeout_min * 60, cfg.limits.idle_grace_min * 60)
        self._update_watchdog_text()

    def _update_legend(self, keys: dict[str, str]) -> None:
        lines = []
        for control, what, action in LEGEND:
            key = keys.get(action, "") if action else ""
            key = QKeySequence(key, QKeySequence.PortableText).toString(QKeySequence.NativeText) if key else ""
            text = f"{control} - {what}" + (f"  <span style='color:#666'>({key})</span>" if key else "")
            if action == "cancel_motion":
                text = f"<span style='color:{RED}'>{text}</span>"
            lines.append(text)
        self.legend_label.setText("<br>".join(lines))

    def stop_all(self) -> None:
        """Stop button: cancel all motion and switch the heater (PSU output) off."""
        self.c.cancel_motion.emit()
        self.c.set_psu_enabled.emit(False)
        self._notify("STOP: motion cancelled, PSU output off")

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
        self.watchdog.stop()
        self.idle_dialog.hide()
        self.input_map.uninstall()
        # Shutdown blocks for a few seconds (Kinesis Disconnect ~0.6 s per stage, then the Kinesis
        # USB manager takes ~2 s to unload at process exit). Hide first so the click feels instant.
        self.hide()
        QApplication.processEvents()
        self.c.shutdown()
        self.log_tab.detach()
        ev.accept()
