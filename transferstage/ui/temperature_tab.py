"""Temperature panels, laid out like the LabVIEW front panel (gui_v2.vi).

``TemperaturePanel`` owns the thermal widgets and hands them to the main window:

* ``control_panel``  – left column "Temperature Control": ramp rate, Ramp on/off, set point, Set, PSU output
* ``settings_page``  – centre tab "Settings": SN#, PID period / reinitialize, PID gains, PV,
                       program SP, DMM / PSU, ramp control readouts
* ``profile_page``   – centre tab "Profile": multi-segment ramp/hold editor
* ``autotune_page``  – centre tab "Autotune": relay autotune
* ``charts``         – right column: PV/SP legend, temperature chart, voltage chart
* ``alarm_banner``   – red banner shown above everything while an alarm is active
"""

from __future__ import annotations

import logging
import time
from collections import deque

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import QObject, Qt, pyqtSignal
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
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..app import AppController
from ..config import HARD_T_MAX_C
from ..control.autotune import RECOMMENDED_RULE, RULES, AutotuneParams, AutotuneState, AutotuneStatus
from ..control.profile import ProfileError, Segment, TemperatureProfile
from ..control.thermal import ThermalSample
from .widgets import AMBER, GREEN, LED_GREEN_OFF, RED, Led, fmt, indicator, section, spin

log = logging.getLogger(__name__)

MAX_POINTS = 200_000  # history kept for the charts (~28 h at 2 Hz)
PV_COLOR = "#1f77d4"
SP_COLOR = "#e0303a"


def _clock_axis() -> pg.DateAxisItem:
    """Local clock-time axis whose finest labels are HH:MM:SS (no SS.mmm on short spans)."""
    axis = pg.DateAxisItem()
    axis.zoomLevels.pop(1, None)  # pyqtgraph's sub-second zoom level
    return axis


class TemperaturePanel(QObject):
    message = pyqtSignal(str)

    def __init__(self, controller: AppController, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.c = controller
        cfg = controller.config
        self.t_max = min(cfg.limits.t_max_c, HARD_T_MAX_C)
        self._last: ThermalSample | None = None
        self._last_rate = cfg.ramp.default_rate_deg_min
        self._t: deque[float] = deque(maxlen=MAX_POINTS)
        self._temp: deque[float] = deque(maxlen=MAX_POINTS)
        self._target: deque[float] = deque(maxlen=MAX_POINTS)
        self._prog: deque[float] = deque(maxlen=MAX_POINTS)
        self._volt: deque[float] = deque(maxlen=MAX_POINTS)
        # Chart x = wall0 + sample.t: epoch seconds for the clock-time axis, but advancing on
        # the worker's monotonic clock so an NTP/DST change can't make the trace jump.
        self._wall0: float | None = None
        self._last_raw_t: float | None = None

        self.alarm_banner = self._build_alarm_banner()
        self.control_panel = self._build_control_panel()
        self.settings_page = self._build_settings_page()
        self.profile_page = self._build_profile_page()
        self.autotune_page = self._build_autotune_page()
        self.charts = self._build_charts()

    # ------------------------------------------------------------------ layout

    def _build_alarm_banner(self) -> QFrame:
        banner = QFrame()
        banner.setStyleSheet(f"QFrame {{ background:{RED}; }} QLabel {{ color:white; font-weight:bold; }}")
        h = QHBoxLayout(banner)
        self.alarm_label = QLabel("")
        self.alarm_label.setWordWrap(True)
        self.ack_btn = QPushButton("Acknowledge")
        self.ack_btn.clicked.connect(self.c.acknowledge_alarm.emit)
        h.addWidget(self.alarm_label, 1)
        h.addWidget(self.ack_btn)
        banner.setVisible(False)
        return banner

    def _build_control_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        self.busy_led = Led("Busy", GREEN, off_color=LED_GREEN_OFF)
        self.busy_led.setToolTip("On while the PSU output is on (PID driving the heater)")
        v.addWidget(section("Temperature Control", extra=self.busy_led))

        grid = QGridLayout()
        self.rate_spin = spin(0, 1000, self._last_rate, decimals=2, step=1)
        self.rate_spin.setToolTip("°C/min; 0 = step change")
        self.sp_spin = spin(0, self.t_max, 20.0, decimals=2, step=1)
        self.sp_spin.setToolTip("°C")
        self.ramp_btn = QPushButton()
        self.ramp_btn.setCheckable(True)
        self.ramp_btn.setToolTip(
            "On: Set ramps the set point at the ramp rate.\nOff: Set jumps the set point straight to the target."
        )
        self.ramp_btn.setStyleSheet(
            f"QPushButton {{ font-weight:bold; }} QPushButton:checked {{ background:{AMBER}; color:black; }}"
        )
        self.ramp_btn.toggled.connect(self._on_ramp_toggled)
        self.ramp_btn.setChecked(True)
        self.set_btn = QPushButton("✔ Set")
        self.set_btn.setMinimumHeight(34)
        self.set_btn.clicked.connect(self.apply_setpoint)
        grid.addWidget(QLabel("Ramp rate (deg/min)"), 0, 0, Qt.AlignRight)
        grid.addWidget(self.rate_spin, 0, 1)
        grid.addWidget(self.ramp_btn, 0, 2)
        grid.addWidget(QLabel("setpoint (°C)"), 1, 0, Qt.AlignRight)
        grid.addWidget(self.sp_spin, 1, 1)
        grid.addWidget(self.set_btn, 1, 2)
        v.addLayout(grid)

        h = QHBoxLayout()
        self.psu_btn = QPushButton("PSU Output: Off")
        self.psu_btn.setCheckable(True)
        self.psu_btn.setMinimumHeight(32)
        self.psu_btn.setStyleSheet(
            f"QPushButton {{ font-weight:bold; }} QPushButton:checked {{ background:{GREEN}; color:black; }}"
        )
        self.psu_btn.clicked.connect(self._on_psu_clicked)
        h.addWidget(self.psu_btn, 1)
        h.addWidget(QLabel("PV"))
        self.pv_left = indicator("—", 80)
        h.addWidget(self.pv_left)
        v.addLayout(h)
        return w

    def _build_settings_page(self) -> QWidget:
        d = self.c.config.devices
        p = self.c.config.pid
        w = QWidget()
        grid = QGridLayout(w)

        # left half: devices + readouts + ramp control
        left = QVBoxLayout()
        left.addWidget(QLabel("Motor SN#"))
        self.motor_sn = indicator(d.z_serial, 120)
        left.addWidget(self.motor_sn)
        left.addWidget(QLabel("Rotator SN#"))
        self.rot_sn = indicator(d.rotator_serial, 120)
        left.addWidget(self.rot_sn)
        left.addSpacing(10)

        self.ro: dict[str, QLabel] = {}
        form = QFormLayout()
        for key, label in (
            ("T", "PV"),
            ("prog", "program SP"),
            ("target", "target SP"),
            ("vcmd", "V command"),
            ("vread", "V readback"),
            ("iread", "I readback"),
        ):
            lab = indicator("—", 100)
            self.ro[key] = lab
            form.addRow(label, lab)
        form.addRow("DMM", indicator(d.dmm_visa, 100))
        form.addRow("PSU", indicator(d.psu_port, 100))
        left.addLayout(form)

        rc = QGroupBox("Ramp Control")
        rf = QFormLayout(rc)
        self.rc_og = indicator("—", 80)
        self.rc_rate = indicator("—", 80)
        self.rc_sp = indicator("—", 80)
        self.ascending_led = Led("", GREEN, off_color=LED_GREEN_OFF)
        self.ramping_led = Led("", AMBER)
        self.ramping_led.setToolTip("On while the program set point is still moving toward the target")
        rf.addRow("Ramping?", self.ramping_led)
        rf.addRow("OG setpoint", self.rc_og)
        rf.addRow("Ramp rate (deg/min)", self.rc_rate)
        rf.addRow("setpoint", self.rc_sp)
        rf.addRow("Ascending?", self.ascending_led)
        left.addWidget(rc)
        left.addStretch(1)
        grid.addLayout(left, 0, 0)

        # right half: PID period / reinitialize / gains
        right = QVBoxLayout()
        top = QGridLayout()
        top.addWidget(QLabel("PID millisecond multiple"), 0, 0)
        self.period_label = indicator(f"{p.period_ms}", 70)
        self.period_label.setToolTip("Control period in ms (change it in the Config tab; applies on restart)")
        top.addWidget(self.period_label, 1, 0)
        top.addWidget(QLabel("PID reinitialize?"), 0, 1)
        self.reset_btn = QPushButton("Reinitialize")
        self.reset_btn.clicked.connect(self.c.reset_pid.emit)
        top.addWidget(self.reset_btn, 1, 1)
        right.addLayout(top)
        right.addSpacing(10)

        gains = QGroupBox("PID gains")
        gains.setStyleSheet("QGroupBox { background:#9a9a9a; } QLabel { color:black; }")
        gf = QFormLayout(gains)
        self.kc_spin = spin(0, 1000, p.kc, decimals=3, step=0.05)
        self.ti_spin = spin(0, 1000, p.ti_min, decimals=3, step=0.1)
        self.td_spin = spin(0, 1000, p.td_min, decimals=3, step=0.01)
        self.ti_spin.setToolTip("Integral time in minutes (0 disables integral)")
        gf.addRow("proportional gain (Kc)", self.kc_spin)
        gf.addRow("integral time (Ti, min)", self.ti_spin)
        gf.addRow("derivative time (Td, min)", self.td_spin)
        self.gains_btn = QPushButton("Apply gains")
        self.gains_btn.clicked.connect(
            lambda: self.c.set_gains.emit(self.kc_spin.value(), self.ti_spin.value(), self.td_spin.value())
        )
        gf.addRow("", self.gains_btn)
        right.addWidget(gains)
        right.addStretch(1)
        grid.addLayout(right, 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        return w

    def _build_profile_page(self) -> QWidget:
        g = QWidget()
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

    def _build_autotune_page(self) -> QWidget:
        g = QWidget()
        v = QVBoxLayout(g)
        form = QFormLayout()
        self.at_sp = spin(0, self.t_max - 1, 100.0, decimals=1, step=1, suffix=" °C")
        self.at_bias = spin(0, 24, 0.0, decimals=2, step=0.5, suffix=" V")
        self.at_bias.setSpecialValueText("Present output")
        self.at_bias.setToolTip("Relay centre voltage; 0 = the present PSU output (hold the set point first)")
        self.at_amp = spin(0.1, 24, 3.0, decimals=2, step=0.5, suffix=" V")
        self.at_amp.setToolTip("Relay swings bias ± amplitude, clamped to 0 V and the voltage limit")
        self.at_hyst = spin(0, 10, 0.3, decimals=2, step=0.1, suffix=" °C")
        self.at_hyst.setToolTip("Hysteresis band; set a little above the temperature noise")
        self.at_cycles = QSpinBox()
        self.at_cycles.setRange(1, 20)
        self.at_cycles.setValue(3)
        self.at_timeout = spin(1, 600, 60, decimals=0, step=5, suffix=" min")
        self.at_maxdev = spin(1, 100, 30, decimals=1, step=1, suffix=" °C")
        self.at_maxdev.setToolTip("Abort if the temperature rises more than this above the set point")
        for label, w in (
            ("Set point", self.at_sp),
            ("Bias", self.at_bias),
            ("Amplitude", self.at_amp),
            ("Hysteresis", self.at_hyst),
            ("Cycles", self.at_cycles),
            ("Timeout", self.at_timeout),
            ("Max rise above SP", self.at_maxdev),
        ):
            form.addRow(label + ":", w)
        v.addLayout(form)
        h = QHBoxLayout()
        self.at_start_btn = QPushButton("Autotune")
        self.at_start_btn.clicked.connect(self.start_autotune)
        self.at_cancel_btn = QPushButton("Cancel")
        self.at_cancel_btn.setEnabled(False)
        self.at_cancel_btn.clicked.connect(self.c.cancel_autotune.emit)
        h.addWidget(self.at_start_btn)
        h.addWidget(self.at_cancel_btn)
        v.addLayout(h)
        self.at_progress = QProgressBar()
        self.at_progress.setRange(0, 100)
        self.at_progress.setValue(0)
        v.addWidget(self.at_progress)
        self.at_status = QLabel("Autotune: idle")
        self.at_status.setWordWrap(True)
        v.addWidget(self.at_status)
        self.at_table = QTableWidget(len(RULES), 3)
        self.at_table.setHorizontalHeaderLabels(["Kc (V/°C)", "Ti (min)", "Td (min)"])
        self.at_table.setVerticalHeaderLabels(list(RULES))
        self.at_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.at_table.verticalHeader().setDefaultSectionSize(22)
        self.at_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.at_table.setSelectionMode(QTableWidget.SingleSelection)
        self.at_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.at_table.setMinimumHeight(len(RULES) * 22 + 30)
        v.addWidget(self.at_table)
        self.at_apply_btn = QPushButton("Apply gains")
        self.at_apply_btn.setToolTip("Copy the selected row into the PID gains and apply them")
        self.at_apply_btn.setEnabled(False)
        self.at_apply_btn.clicked.connect(self.apply_autotune_gains)
        v.addWidget(self.at_apply_btn)
        v.addStretch(1)
        self._at_result = None
        return g

    def _build_charts(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)

        # PV / SP legend with live values (top-right of the LabVIEW panel)
        legend = QHBoxLayout()
        legend.addStretch(1)
        lg = QGridLayout()
        self.pv_label = indicator("—", 80)
        self.sp_label = indicator("—", 80)
        for row, (name, color, lab) in enumerate((("PV", PV_COLOR, self.pv_label), ("SP", SP_COLOR, self.sp_label))):
            swatch = QLabel(f'{name} <span style="color:{color}; font-size:16px;">&#8212;&#8212;</span>')
            swatch.setTextFormat(Qt.RichText)
            lg.addWidget(swatch, row, 0)
            lg.addWidget(lab, row, 1)
        legend.addLayout(lg)
        v.addLayout(legend)

        pg.setConfigOptions(antialias=True)
        self.temp_plot = pg.PlotWidget(background="w", axisItems={"bottom": _clock_axis()})
        self.temp_plot.setLabel("left", "Temperature", units="°C")
        self.temp_plot.showGrid(x=True, y=True, alpha=0.25)
        self.curve_T = self.temp_plot.plot(pen=pg.mkPen(PV_COLOR, width=2), name="PV")
        self.curve_prog = self.temp_plot.plot(pen=pg.mkPen(SP_COLOR, width=1.5), name="SP")
        self.curve_target = self.temp_plot.plot(
            pen=pg.mkPen(SP_COLOR, width=1, style=Qt.DashLine), name="Target SP"
        )
        self.volt_plot = pg.PlotWidget(background="w", axisItems={"bottom": _clock_axis()})
        self.volt_plot.setLabel("left", "Voltage", units="V")
        self.volt_plot.setLabel("bottom", "Time")
        self.volt_plot.showGrid(x=True, y=True, alpha=0.25)
        self.volt_plot.setXLink(self.temp_plot)
        self.curve_V = self.volt_plot.plot(pen=pg.mkPen("#2a8a2a", width=2), name="V")
        for plot in (self.temp_plot, self.volt_plot):
            for ax in ("left", "bottom"):
                plot.getAxis(ax).setTextPen("k")
                plot.getAxis(ax).setPen("k")
        v.addWidget(self.temp_plot, 3)
        v.addWidget(self.volt_plot, 2)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Window:"))
        self.window_spin = spin(0.5, 24 * 60, 2, decimals=1, step=1, suffix=" min")
        self.window_spin.valueChanged.connect(lambda _: self.redraw())
        bar.addWidget(self.window_spin)
        self.pause_chk = QCheckBox("Pause")
        self.pause_chk.setToolTip("Pause, then drag to pan / wheel to zoom")
        self.pause_chk.toggled.connect(lambda on: None if on else self.redraw())
        bar.addWidget(self.pause_chk)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear_charts)
        bar.addWidget(clear)
        bar.addStretch(1)
        v.addLayout(bar)
        return w

    # ------------------------------------------------------------------ commands

    def apply_setpoint(self) -> None:
        sp = float(self.sp_spin.value())
        self._last_rate = float(self.rate_spin.value()) if self.ramp_btn.isChecked() else 0.0
        self.c.set_target.emit(sp, self._last_rate)
        if self._last_rate > 0:
            self.message.emit(f"Set point {sp:g} °C, ramping at {self._last_rate:g} °C/min")
        else:
            self.message.emit(f"Set point {sp:g} °C (step, no ramp)")

    def _on_ramp_toggled(self, on: bool) -> None:
        self.ramp_btn.setText("Ramp: On" if on else "Ramp: Off")
        self.rate_spin.setEnabled(on)

    def _on_psu_clicked(self, checked: bool) -> None:
        self.c.set_psu_enabled.emit(checked)
        self._set_psu_button(checked)

    def _set_psu_button(self, on: bool) -> None:
        self.psu_btn.blockSignals(True)
        self.psu_btn.setChecked(on)
        self.psu_btn.blockSignals(False)
        self.psu_btn.setText("PSU Output: On" if on else "PSU Output: Off")

    # ------------------------------------------------------------------ autotune

    def autotune_params(self) -> AutotuneParams:
        bias = self.at_bias.value()
        return AutotuneParams(
            setpoint_c=float(self.at_sp.value()),
            bias_v=None if bias == 0 else float(bias),
            amplitude_v=float(self.at_amp.value()),
            hysteresis_c=float(self.at_hyst.value()),
            cycles=int(self.at_cycles.value()),
            timeout_s=float(self.at_timeout.value()) * 60.0,
            max_deviation_c=float(self.at_maxdev.value()),
        )

    def start_autotune(self) -> None:
        params = self.autotune_params()
        try:
            params.validate(self.t_max)
        except ValueError as exc:
            QMessageBox.warning(self.autotune_page, "Autotune", str(exc))
            return
        self._at_result = None
        self.at_apply_btn.setEnabled(False)
        self.at_table.clearContents()
        self.at_progress.setValue(0)
        self.at_status.setText("Autotune: starting…")
        self.c.start_autotune.emit(params)

    def on_autotune_progress(self, st: AutotuneStatus) -> None:
        self.at_start_btn.setEnabled(False)
        self.at_cancel_btn.setEnabled(True)
        self.at_progress.setValue(int(round(st.fraction * 100)))
        what = "approaching set point" if st.state is AutotuneState.APPROACH else "relay oscillation"
        self.at_status.setText(
            f"Autotune: {what}, cycle {st.cycles_done}/{st.cycles_total}, "
            f"{st.elapsed_s / 60:.1f} min, output {st.output_v:.2f} V"
        )

    def on_autotune_finished(self, st: AutotuneStatus) -> None:
        self.at_start_btn.setEnabled(True)
        self.at_cancel_btn.setEnabled(False)
        if st.state is not AutotuneState.DONE or st.result is None:
            self.at_status.setText(f"Autotune aborted: {st.message}")
            self.message.emit(f"Autotune aborted: {st.message}")
            return
        r = st.result
        self._at_result = r
        self.at_progress.setValue(100)
        for row, name in enumerate(RULES):
            g = r.gains[name]
            for col, val in enumerate((g.kc, g.ti_min, g.td_min)):
                self.at_table.setItem(row, col, QTableWidgetItem(f"{val:.4g}"))
        self.at_table.selectRow(RULES.index(RECOMMENDED_RULE))
        self.at_apply_btn.setEnabled(True)
        self.at_status.setText(
            f"Autotune done: Ku = {r.ku:.4g} V/°C, Pu = {r.pu_s:.1f} s, a = {r.amplitude_c:.2f} °C "
            f"(period spread {r.period_spread:.0%}). The PID now holds the set point with the old gains."
        )
        self.message.emit(f"Autotune done: Ku = {r.ku:.4g} V/°C, Pu = {r.pu_s:.1f} s")

    def apply_autotune_gains(self) -> None:
        if self._at_result is None:
            return
        rows = self.at_table.selectionModel().selectedRows()
        row = rows[0].row() if rows else RULES.index(RECOMMENDED_RULE)
        name = RULES[row]
        g = self._at_result.gains[name]
        self.kc_spin.setValue(g.kc)
        self.ti_spin.setValue(g.ti_min)
        self.td_spin.setValue(g.td_min)
        self.c.set_gains.emit(g.kc, g.ti_min, g.td_min)
        self.message.emit(f"PID gains ({name}): Kc {g.kc:.4g}, Ti {g.ti_min:.4g} min, Td {g.td_min:.4g} min")

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
            QMessageBox.warning(self.profile_page, "Profile", str(exc))
            return
        self.c.start_profile.emit(profile)
        self.message.emit(f"Profile started ({len(profile.segments)} segments)")

    def _load_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self.profile_page, "Load profile", "", "YAML (*.yaml *.yml)")
        if not path:
            return
        try:
            self.set_profile(TemperatureProfile.load(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self.profile_page, "Load profile", str(exc))

    def _save_profile(self) -> None:
        try:
            profile = self.profile_from_table()
        except (ProfileError, ValueError) as exc:
            QMessageBox.warning(self.profile_page, "Profile", str(exc))
            return
        path, _ = QFileDialog.getSaveFileName(self.profile_page, "Save profile", "profile.yaml", "YAML (*.yaml *.yml)")
        if path:
            try:
                profile.save(path)
            except OSError as exc:
                QMessageBox.warning(self.profile_page, "Save profile", str(exc))

    # ------------------------------------------------------------------ samples

    def on_sample(self, s: ThermalSample) -> None:
        self._last = s
        self.ro["T"].setText(fmt(s.temperature_c, 2, "°C"))
        self.ro["target"].setText(fmt(s.target_sp_c, 2, "°C"))
        self.ro["prog"].setText(fmt(s.program_sp_c, 2, "°C"))
        self.ro["vcmd"].setText(fmt(s.v_cmd, 3, "V"))
        self.ro["vread"].setText(fmt(s.v_read, 3, "V"))
        self.ro["iread"].setText(fmt(s.i_read, 3, "A"))
        self.pv_label.setText(fmt(s.temperature_c, 2))
        self.pv_left.setText(fmt(s.temperature_c, 2, "°C"))
        self.sp_label.setText(fmt(s.program_sp_c, 2))
        self.rc_og.setText(fmt(s.target_sp_c, 2))
        self.rc_rate.setText(fmt(self._last_rate, 2))
        self.rc_sp.setText(fmt(s.program_sp_c, 2))
        self.ascending_led.set_on(s.target_sp_c >= s.program_sp_c)
        self.ramping_led.set_on(s.ramping)
        self.busy_led.set_on(s.psu_on)
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

        if self._wall0 is None or (self._last_raw_t is not None and s.t < self._last_raw_t):
            # First sample, or a new worker restarted its clock: re-anchor to wall time.
            self._wall0 = time.time() - s.t
        self._last_raw_t = s.t
        self._t.append(self._wall0 + s.t)
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
