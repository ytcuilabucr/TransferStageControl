"""Temperature tab: set point + ramp, PSU on/off, PID gains, readouts, charts, profiles, alarms."""

from __future__ import annotations

import logging
from collections import deque

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..app import AppController
from ..config import HARD_T_MAX_C
from ..control.profile import ProfileError, Segment, TemperatureProfile
from ..control.thermal import ThermalSample
from .widgets import AMBER, GREEN, RED, Led, fmt, spin

log = logging.getLogger(__name__)

MAX_POINTS = 200_000  # history kept for the charts (~28 h at 2 Hz)


class TemperatureTab(QWidget):
    message = pyqtSignal(str)

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.c = controller
        cfg = controller.config
        self.t_max = min(cfg.limits.t_max_c, HARD_T_MAX_C)
        self._last: ThermalSample | None = None
        self._t: deque[float] = deque(maxlen=MAX_POINTS)
        self._temp: deque[float] = deque(maxlen=MAX_POINTS)
        self._target: deque[float] = deque(maxlen=MAX_POINTS)
        self._prog: deque[float] = deque(maxlen=MAX_POINTS)
        self._volt: deque[float] = deque(maxlen=MAX_POINTS)

        root = QVBoxLayout(self)
        root.addWidget(self._build_alarm_banner())

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(self._build_setpoint_group())
        lv.addWidget(self._build_readouts_group())
        lv.addWidget(self._build_pid_group())
        lv.addWidget(self._build_profile_group(), 1)
        split.addWidget(left)
        split.addWidget(self._build_charts())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([380, 900])
        root.addWidget(split, 1)

    # ------------------------------------------------------------------ layout

    def _build_alarm_banner(self) -> QFrame:
        self.alarm_banner = QFrame()
        self.alarm_banner.setStyleSheet(f"QFrame {{ background:{RED}; }} QLabel {{ color:white; font-weight:bold; }}")
        h = QHBoxLayout(self.alarm_banner)
        self.alarm_label = QLabel("")
        self.alarm_label.setWordWrap(True)
        self.ack_btn = QPushButton("Acknowledge")
        self.ack_btn.clicked.connect(self.c.acknowledge_alarm.emit)
        h.addWidget(self.alarm_label, 1)
        h.addWidget(self.ack_btn)
        self.alarm_banner.setVisible(False)
        return self.alarm_banner

    def _build_setpoint_group(self) -> QGroupBox:
        g = QGroupBox("Set point")
        grid = QGridLayout(g)
        self.sp_spin = spin(0, self.t_max, 25.0, decimals=1, step=1, suffix=" °C")
        self.rate_spin = spin(0, 1000, self.c.config.ramp.default_rate_deg_min, decimals=1, step=1, suffix=" °C/min")
        self.rate_spin.setToolTip("0 = step change")
        self.set_btn = QPushButton("Set")
        self.set_btn.clicked.connect(self.apply_setpoint)
        self.ramping_led = Led("Ramping", AMBER)
        self.psu_btn = QPushButton("PSU Off")
        self.psu_btn.setCheckable(True)
        self.psu_btn.setMinimumHeight(36)
        self.psu_btn.setStyleSheet(
            f"QPushButton {{ font-weight:bold; }} QPushButton:checked {{ background:{GREEN}; color:black; }}"
        )
        self.psu_btn.clicked.connect(self._on_psu_clicked)
        grid.addWidget(QLabel("Temperature:"), 0, 0)
        grid.addWidget(self.sp_spin, 0, 1)
        grid.addWidget(QLabel("Ramp rate:"), 1, 0)
        grid.addWidget(self.rate_spin, 1, 1)
        grid.addWidget(self.set_btn, 0, 2, 2, 1)
        grid.addWidget(self.ramping_led, 2, 0)
        grid.addWidget(self.psu_btn, 2, 1, 1, 2)
        return g

    def _build_readouts_group(self) -> QGroupBox:
        g = QGroupBox("Readouts")
        form = QFormLayout(g)
        mono = QFont("Consolas")
        mono.setPointSize(13)
        self.ro: dict[str, QLabel] = {}
        for key, label in (
            ("T", "Temperature"),
            ("target", "Target SP"),
            ("prog", "Program SP"),
            ("vcmd", "V command"),
            ("vread", "V readback"),
            ("iread", "I readback"),
        ):
            lab = QLabel("—")
            lab.setFont(mono)
            self.ro[key] = lab
            form.addRow(label + ":", lab)
        return g

    def _build_pid_group(self) -> QGroupBox:
        g = QGroupBox("PID")
        grid = QGridLayout(g)
        p = self.c.config.pid
        self.kc_spin = spin(0, 1000, p.kc, decimals=4, step=0.05)
        self.ti_spin = spin(0, 1000, p.ti_min, decimals=4, step=0.1, suffix=" min")
        self.td_spin = spin(0, 1000, p.td_min, decimals=4, step=0.01, suffix=" min")
        self.ti_spin.setToolTip("Integral time in minutes (0 disables integral)")
        self.gains_btn = QPushButton("Apply gains")
        self.gains_btn.clicked.connect(
            lambda: self.c.set_gains.emit(self.kc_spin.value(), self.ti_spin.value(), self.td_spin.value())
        )
        self.reset_btn = QPushButton("Reinitialize PID")
        self.reset_btn.clicked.connect(self.c.reset_pid.emit)
        grid.addWidget(QLabel("Kc"), 0, 0)
        grid.addWidget(self.kc_spin, 0, 1)
        grid.addWidget(QLabel("Ti"), 1, 0)
        grid.addWidget(self.ti_spin, 1, 1)
        grid.addWidget(QLabel("Td"), 2, 0)
        grid.addWidget(self.td_spin, 2, 1)
        grid.addWidget(self.gains_btn, 0, 2)
        grid.addWidget(self.reset_btn, 1, 2)
        return g

    def _build_profile_group(self) -> QGroupBox:
        g = QGroupBox("Profile")
        v = QVBoxLayout(g)
        self.profile_table = QTableWidget(0, 3)
        self.profile_table.setHorizontalHeaderLabels(["Ramp to (°C)", "Rate (°C/min)", "Hold (min)"])
        self.profile_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.profile_table.verticalHeader().setDefaultSectionSize(22)
        self.profile_table.setMinimumHeight(110)
        v.addWidget(self.profile_table, 1)
        h1 = QHBoxLayout()
        for text, fn in (
            ("Add", self._add_row),
            ("Remove", self._remove_row),
            ("Load…", self._load_profile),
            ("Save…", self._save_profile),
        ):
            b = QPushButton(text)
            b.clicked.connect(fn)
            h1.addWidget(b)
        v.addLayout(h1)
        h2 = QHBoxLayout()
        self.start_profile_btn = QPushButton("Start profile")
        self.stop_profile_btn = QPushButton("Stop profile")
        self.start_profile_btn.clicked.connect(self.start_profile)
        self.stop_profile_btn.clicked.connect(self.c.stop_profile.emit)
        h2.addWidget(self.start_profile_btn)
        h2.addWidget(self.stop_profile_btn)
        v.addLayout(h2)
        self.profile_status = QLabel("Profile: idle")
        v.addWidget(self.profile_status)
        return g

    def _build_charts(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Window:"))
        self.window_spin = spin(0.5, 24 * 60, 10, decimals=1, step=1, suffix=" min")
        self.window_spin.valueChanged.connect(lambda _: self.redraw())
        bar.addWidget(self.window_spin)
        self.pause_chk = QCheckBox("Pause")
        self.pause_chk.toggled.connect(lambda on: None if on else self.redraw())
        bar.addWidget(self.pause_chk)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear_charts)
        bar.addWidget(clear)
        bar.addStretch(1)
        bar.addWidget(QLabel("Pause, then drag to pan / wheel to zoom"))
        v.addLayout(bar)

        pg.setConfigOptions(antialias=True)
        self.temp_plot = pg.PlotWidget()
        self.temp_plot.setLabel("left", "Temperature", units="°C")
        self.temp_plot.showGrid(x=True, y=True, alpha=0.3)
        self.temp_plot.addLegend(offset=(10, 10))
        self.curve_T = self.temp_plot.plot(pen=pg.mkPen("#e74c3c", width=2), name="T")
        self.curve_target = self.temp_plot.plot(
            pen=pg.mkPen("#2ecc40", width=1, style=Qt.DashLine), name="Target SP"
        )
        self.curve_prog = self.temp_plot.plot(pen=pg.mkPen("#3498db", width=1.5), name="Program SP")
        self.volt_plot = pg.PlotWidget()
        self.volt_plot.setLabel("left", "PSU voltage", units="V")
        self.volt_plot.setLabel("bottom", "Time", units="s")
        self.volt_plot.showGrid(x=True, y=True, alpha=0.3)
        self.volt_plot.setXLink(self.temp_plot)
        self.curve_V = self.volt_plot.plot(pen=pg.mkPen("#f39c12", width=2), name="V")
        v.addWidget(self.temp_plot, 3)
        v.addWidget(self.volt_plot, 2)
        return w

    # ------------------------------------------------------------------ commands

    def apply_setpoint(self) -> None:
        self.c.set_target.emit(float(self.sp_spin.value()), float(self.rate_spin.value()))
        self.message.emit(f"Set point {self.sp_spin.value():g} °C at {self.rate_spin.value():g} °C/min")

    def _on_psu_clicked(self, checked: bool) -> None:
        self.c.set_psu_enabled.emit(checked)
        self._set_psu_button(checked)

    def _set_psu_button(self, on: bool) -> None:
        self.psu_btn.blockSignals(True)
        self.psu_btn.setChecked(on)
        self.psu_btn.blockSignals(False)
        self.psu_btn.setText("PSU On" if on else "PSU Off")

    # ------------------------------------------------------------------ profile editor

    def _add_row(self, seg: Segment | None = None) -> None:
        r = self.profile_table.rowCount()
        self.profile_table.insertRow(r)
        values = (
            (seg.ramp_to, seg.rate_deg_min, seg.hold_min)
            if isinstance(seg, Segment)
            else (self.sp_spin.value(), self.rate_spin.value(), 0.0)
        )
        for col, val in enumerate(values):
            self.profile_table.setItem(r, col, QTableWidgetItem(f"{val:g}"))

    def _remove_row(self) -> None:
        r = self.profile_table.currentRow()
        if r < 0:
            r = self.profile_table.rowCount() - 1
        if r >= 0:
            self.profile_table.removeRow(r)

    def profile_from_table(self) -> TemperatureProfile:
        segs = []
        for r in range(self.profile_table.rowCount()):
            vals = []
            for col in range(3):
                item = self.profile_table.item(r, col)
                text = item.text().strip() if item else ""
                try:
                    vals.append(float(text) if text else 0.0)
                except ValueError as exc:
                    raise ProfileError(f"row {r + 1}, column {col + 1}: {text!r} is not a number") from exc
            if vals[0] > self.t_max:
                raise ProfileError(f"row {r + 1}: {vals[0]:g} °C is above the {self.t_max:g} °C limit")
            segs.append(Segment(*vals))
        if not segs:
            raise ProfileError("the profile has no segments")
        return TemperatureProfile(segs, name="gui")

    def set_profile(self, profile: TemperatureProfile) -> None:
        self.profile_table.setRowCount(0)
        for s in profile.segments:
            self._add_row(s)

    def start_profile(self) -> None:
        try:
            profile = self.profile_from_table()
        except (ProfileError, ValueError) as exc:
            QMessageBox.warning(self, "Profile", str(exc))
            return
        self.c.start_profile.emit(profile)
        self.message.emit(f"Profile started ({len(profile.segments)} segments)")

    def _load_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load profile", "", "YAML (*.yaml *.yml)")
        if not path:
            return
        try:
            self.set_profile(TemperatureProfile.load(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Load profile", str(exc))

    def _save_profile(self) -> None:
        try:
            profile = self.profile_from_table()
        except (ProfileError, ValueError) as exc:
            QMessageBox.warning(self, "Profile", str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save profile", "profile.yaml", "YAML (*.yaml *.yml)")
        if path:
            try:
                profile.save(path)
            except OSError as exc:
                QMessageBox.warning(self, "Save profile", str(exc))

    # ------------------------------------------------------------------ samples

    def on_sample(self, s: ThermalSample) -> None:
        self._last = s
        self.ro["T"].setText(fmt(s.temperature_c, 2, "°C"))
        self.ro["target"].setText(fmt(s.target_sp_c, 1, "°C"))
        self.ro["prog"].setText(fmt(s.program_sp_c, 2, "°C"))
        self.ro["vcmd"].setText(fmt(s.v_cmd, 2, "V"))
        self.ro["vread"].setText(fmt(s.v_read, 2, "V"))
        self.ro["iread"].setText(fmt(s.i_read, 3, "A"))
        self.ramping_led.set_on(s.ramping)
        if not self.psu_btn.isDown():
            self._set_psu_button(s.psu_on)

        if s.profile_segment is not None:
            self.profile_status.setText(f"Profile: segment {s.profile_segment + 1}, {s.profile_phase}")
        else:
            self.profile_status.setText("Profile: idle")

        active = [a for a in s.alarms if a.active or a.is_trip]
        if active:
            self.alarm_label.setText("  |  ".join(f"{a.severity.value.upper()}: {a.message}" for a in active))
            self.alarm_banner.setVisible(True)
            self.ack_btn.setVisible(any(a.is_trip for a in active))
        else:
            self.alarm_banner.setVisible(False)

        self._t.append(s.t)
        self._temp.append(np.nan if s.temperature_c is None else s.temperature_c)
        self._target.append(s.target_sp_c)
        self._prog.append(s.program_sp_c)
        self._volt.append(s.v_read if s.v_read is not None else s.v_cmd)
        if not self.pause_chk.isChecked():
            self.redraw()

    def redraw(self) -> None:
        if not self._t:
            for c in (self.curve_T, self.curve_target, self.curve_prog, self.curve_V):
                c.setData([], [])
            return
        t = np.fromiter(self._t, float, len(self._t))
        t0 = t[-1] - self.window_spin.value() * 60.0
        i = int(np.searchsorted(t, t0))
        tt = t[i:]

        def tail(d: deque) -> np.ndarray:
            return np.fromiter(d, float, len(d))[i:]

        self.curve_T.setData(tt, tail(self._temp), connect="finite")
        self.curve_target.setData(tt, tail(self._target))
        self.curve_prog.setData(tt, tail(self._prog))
        self.curve_V.setData(tt, tail(self._volt))
        self.temp_plot.setXRange(max(t0, t[0]), t[-1], padding=0.01)

    def clear_charts(self) -> None:
        for d in (self._t, self._temp, self._target, self._prog, self._volt):
            d.clear()
        self.redraw()
