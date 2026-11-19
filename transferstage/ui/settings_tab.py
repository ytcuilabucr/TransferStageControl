"""Settings tab: edit and save config.yaml.

Key bindings and the idle-watchdog times apply immediately on Save; everything else (devices, simulate
flags, PID defaults and period, limits, logging) takes effect on restart.
The limit fields cannot be set above the hard limits (300 °C, 24 V).
"""

from __future__ import annotations

import copy
import dataclasses
import logging

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..app import AppController
from ..config import HARD_T_MAX_C, HARD_V_MAX, Config, ConfigError, config_from_dict
from .input_map import ACTIONS
from .widgets import spin

log = logging.getLogger(__name__)

KEY_HINTS = {
    "safe_lock": "TourBox Side + Top combo (toggle Safe Lock)",
    "cancel_motion": "TourBox Top",
    "speed_up": "TourBox Knob clockwise",
    "speed_down": "TourBox Knob counter-clockwise",
    "jog_up": "TourBox Tall (hold)",
    "jog_down": "TourBox Short (hold)",
    "mark": "Event mark in the session CSV (any free TourBox button)",
    "setpoint_up": "TourBox Dial clockwise (setpoint +1 step, not applied)",
    "setpoint_down": "TourBox Dial counter-clockwise (setpoint −1 step)",
    "setpoint_set": "TourBox C1 (apply the setpoint, like ✔ Set)",
    "ramp_toggle": "TourBox C2 (Ramp: On / Off)",
    "side_alone": "TourBox Side alone (no action; reminds you to press Side + Top)",
}


def _com_ports() -> list[str]:
    try:
        from serial.tools import list_ports

        return sorted(p.device for p in list_ports.comports())
    except Exception:  # noqa: BLE001
        return []


class SettingsTab(QWidget):
    saved = pyqtSignal(object)  # Config written to disk
    message = pyqtSignal(str)

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.c = controller

        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        grid = QGridLayout(body)
        grid.addWidget(self._build_devices(), 0, 0)
        grid.addWidget(self._build_pid(), 1, 0)
        grid.addWidget(self._build_limits(), 0, 1)
        grid.addWidget(self._build_logging(), 1, 1)
        grid.addWidget(self._build_keys(), 2, 0, 1, 2)
        grid.setRowStretch(3, 1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        bar = QHBoxLayout()
        bar.addWidget(
            QLabel("Key bindings and the Dial step apply on Save. Device, simulate, PID, limit and logging changes take effect on restart.")
        )
        bar.addStretch(1)
        self.revert_btn = QPushButton("Revert")
        self.save_btn = QPushButton("Save")
        self.revert_btn.clicked.connect(self.load_from_config)
        self.save_btn.clicked.connect(self.save)
        bar.addWidget(self.revert_btn)
        bar.addWidget(self.save_btn)
        outer.addLayout(bar)

        self.load_from_config()

    # ------------------------------------------------------------------ layout

    def _build_devices(self) -> QGroupBox:
        g = QGroupBox("Devices")
        f = QFormLayout(g)
        self.z_serial = QLineEdit()
        self.rot_serial = QLineEdit()
        self.psu_port = QComboBox()
        self.psu_port.setEditable(True)
        self.psu_port.addItems(_com_ports())
        self.dmm_visa = QLineEdit()
        self.kinesis_path = QLineEdit()
        f.addRow("Motor SN# (Z):", self.z_serial)
        f.addRow("Rotator SN#:", self.rot_serial)
        f.addRow("PSU port:", self.psu_port)
        f.addRow("DMM VISA alias:", self.dmm_visa)
        f.addRow("Kinesis folder:", self.kinesis_path)
        self.sim: dict[str, QCheckBox] = {}
        sim_row = QHBoxLayout()
        for name in ("z", "rotator", "psu", "dmm"):
            cb = QCheckBox(name)
            self.sim[name] = cb
            sim_row.addWidget(cb)
        f.addRow("Simulate:", sim_row)
        return g

    def _build_pid(self) -> QGroupBox:
        g = QGroupBox("PID defaults and ramp")
        f = QFormLayout(g)
        self.kc = spin(0, 1000, 0, decimals=4, step=0.05)
        self.ti = spin(0, 1000, 0, decimals=4, step=0.1, suffix=" min")
        self.td = spin(0, 1000, 0, decimals=4, step=0.01, suffix=" min")
        self.period = QSpinBox()
        self.period.setRange(50, 60000)
        self.period.setSuffix(" ms")
        self.out_lo = spin(0, HARD_V_MAX, 0, decimals=2, step=0.5, suffix=" V")
        self.out_hi = spin(0, HARD_V_MAX, 0, decimals=2, step=0.5, suffix=" V")
        self.rate = spin(0, 1000, 0, decimals=1, step=1, suffix=" °C/min")
        self.dial_step = spin(0.01, 100, 1, decimals=2, step=0.5, suffix=" °C")
        self.dial_step.setToolTip("Setpoint change per TourBox Dial click (applies on Save)")
        self.set_enables_psu = QCheckBox("Set switches the PSU output on")
        self.set_enables_psu.setToolTip(
            "Set (TourBox C1) also turns the PSU output on when the new set point is more than 5 °C\n"
            "above the feedforward T_amb, also when ramping down (a controlled cool-down needs heater power).\n"
            "Never while a safety trip is latched. (applies on Save)"
        )
        self.ff_k = spin(0, 1000, 0, decimals=4, step=0.05, suffix=" °C/V²")
        self.ff_k.setSpecialValueText("Off")
        self.ff_k.setToolTip("Feedforward heater model T - T_amb = k·V² (0 = no feedforward)")
        self.ff_tamb = spin(-50, 100, 22, decimals=1, step=1, suffix=" °C")
        self.ff_tamb.setToolTip("Ambient temperature of the feedforward model")
        self.ff_tau = spin(0, 100000, 0, decimals=0, step=10, suffix=" s")
        self.ff_tau.setToolTip("Thermal time constant of the feedforward model; adds the power a ramp needs (0 = off)")
        f.addRow("Kc:", self.kc)
        f.addRow("Ti:", self.ti)
        f.addRow("Td:", self.td)
        f.addRow("PID period:", self.period)
        f.addRow("Output low:", self.out_lo)
        f.addRow("Output high:", self.out_hi)
        f.addRow("Default ramp rate:", self.rate)
        f.addRow("Dial setpoint step:", self.dial_step)
        f.addRow("", self.set_enables_psu)
        f.addRow("Feedforward k:", self.ff_k)
        f.addRow("Feedforward T_amb:", self.ff_tamb)
        f.addRow("Feedforward tau:", self.ff_tau)
        return g

    def _build_limits(self) -> QGroupBox:
        g = QGroupBox(f"Safety limits (hard caps: {HARD_T_MAX_C:g} °C, {HARD_V_MAX:g} V)")
        f = QFormLayout(g)
        self.t_max = spin(1, HARD_T_MAX_C, HARD_T_MAX_C, decimals=1, step=5, suffix=" °C")
        self.v_max = spin(0.1, HARD_V_MAX, HARD_V_MAX, decimals=2, step=0.5, suffix=" V")
        self.v_max.valueChanged.connect(lambda v: self.out_hi.setMaximum(v))
        self.i_max = spin(0.01, 5, 3, decimals=2, step=0.1, suffix=" A")
        self.z_min = spin(-1e6, 1e6, 0, decimals=1, step=100, suffix=" µm")
        self.z_max = spin(-1e6, 1e6, 0, decimals=1, step=100, suffix=" µm")
        self.stale = spin(0.1, 600, 5, decimals=1, step=1, suffix=" s")
        self.idle_timeout = spin(1, 1440, 20, decimals=1, step=5, suffix=" min")
        self.idle_timeout.setToolTip("Heater on and no keyboard/mouse input on this PC for this long -> idle alarm")
        self.idle_grace = spin(0.5, 60, 3, decimals=1, step=1, suffix=" min")
        self.idle_grace.setToolTip("Idle alarm not answered within this time -> heater off")
        f.addRow("Max temperature:", self.t_max)
        f.addRow("Max PSU voltage:", self.v_max)
        f.addRow("PSU current limit:", self.i_max)
        f.addRow("Z soft limit min:", self.z_min)
        f.addRow("Z soft limit max:", self.z_max)
        f.addRow("Stale temperature:", self.stale)
        f.addRow("Idle alarm after:", self.idle_timeout)
        f.addRow("Idle heater-off after:", self.idle_grace)
        return g

    def _build_logging(self) -> QGroupBox:
        g = QGroupBox("Data logging")
        f = QFormLayout(g)
        self.log_dir = QLineEdit()
        self.log_interval = spin(0.05, 3600, 1, decimals=2, step=0.5, suffix=" s")
        f.addRow("Folder:", self.log_dir)
        f.addRow("Interval:", self.log_interval)
        return g

    def _build_keys(self) -> QGroupBox:
        g = QGroupBox("Key bindings (click a field and press the key the TourBox sends)")
        grid = QGridLayout(g)
        self.key_edits: dict[str, QKeySequenceEdit] = {}
        for row, action in enumerate(ACTIONS):
            edit = QKeySequenceEdit()
            clear = QPushButton("Clear")
            clear.clicked.connect(edit.clear)
            self.key_edits[action] = edit
            grid.addWidget(QLabel(action), row, 0)
            grid.addWidget(edit, row, 1)
            grid.addWidget(clear, row, 2)
            grid.addWidget(QLabel(KEY_HINTS.get(action, "")), row, 3)
        grid.setColumnStretch(1, 1)
        return g

    # ------------------------------------------------------------------ load / save

    def load_from_config(self, cfg: Config | None = None) -> None:
        cfg = cfg or self.c.saved_config
        d, p, lim = cfg.devices, cfg.pid, cfg.limits
        self.z_serial.setText(d.z_serial)
        self.rot_serial.setText(d.rotator_serial)
        self.psu_port.setCurrentText(d.psu_port)
        self.dmm_visa.setText(d.dmm_visa)
        self.kinesis_path.setText(d.kinesis_path)
        for name, cb in self.sim.items():
            cb.setChecked(getattr(d.simulate, name))
        self.v_max.setValue(lim.v_max)
        self.kc.setValue(p.kc)
        self.ti.setValue(p.ti_min)
        self.td.setValue(p.td_min)
        self.period.setValue(p.period_ms)
        self.out_lo.setValue(p.output_low)
        self.out_hi.setValue(p.output_high)
        self.ff_k.setValue(p.ff_k_c_per_v2)
        self.ff_tamb.setValue(p.ff_t_amb_c)
        self.ff_tau.setValue(p.ff_tau_s)
        self.rate.setValue(cfg.ramp.default_rate_deg_min)
        self.dial_step.setValue(cfg.ramp.dial_step_c)
        self.set_enables_psu.setChecked(cfg.ramp.set_enables_psu)
        self.t_max.setValue(lim.t_max_c)
        self.i_max.setValue(lim.i_max)
        self.z_min.setValue(lim.z_min_um)
        self.z_max.setValue(lim.z_max_um)
        self.stale.setValue(lim.stale_temp_s)
        self.idle_timeout.setValue(lim.idle_timeout_min)
        self.idle_grace.setValue(lim.idle_grace_min)
        self.log_dir.setText(cfg.logging.dir)
        self.log_interval.setValue(cfg.logging.interval_s)
        for action, edit in self.key_edits.items():
            edit.setKeySequence(QKeySequence(cfg.keys.get(action, ""), QKeySequence.PortableText))

    def to_config(self) -> Config:
        """Build and validate a Config from the form (presets kept from the saved config)."""
        base = copy.deepcopy(self.c.saved_config)
        keys = dict(base.keys)
        for action, edit in self.key_edits.items():
            seq = edit.keySequence()
            # Only the first chord counts: shortcuts are single key combinations.
            keys[action] = QKeySequence(seq[0]).toString(QKeySequence.PortableText) if seq.count() else ""
        data = {
            "devices": {
                "z_serial": self.z_serial.text().strip(),
                "rotator_serial": self.rot_serial.text().strip(),
                "psu_port": self.psu_port.currentText().strip(),
                "dmm_visa": self.dmm_visa.text().strip(),
                "kinesis_path": self.kinesis_path.text().strip(),
                "simulate": {n: cb.isChecked() for n, cb in self.sim.items()},
            },
            "pid": {
                "kc": self.kc.value(),
                "ti_min": self.ti.value(),
                "td_min": self.td.value(),
                "period_ms": int(self.period.value()),
                "output_low": self.out_lo.value(),
                "output_high": self.out_hi.value(),
                "ff_k_c_per_v2": self.ff_k.value(),
                "ff_t_amb_c": self.ff_tamb.value(),
                "ff_tau_s": self.ff_tau.value(),
            },
            "ramp": {
                "default_rate_deg_min": self.rate.value(),
                "dial_step_c": self.dial_step.value(),
                "set_enables_psu": self.set_enables_psu.isChecked(),
            },
            "motion": dataclasses.asdict(base.motion),
            "limits": {
                "t_max_c": min(self.t_max.value(), HARD_T_MAX_C),
                "v_max": min(self.v_max.value(), HARD_V_MAX),
                "i_max": self.i_max.value(),
                "z_min_um": self.z_min.value(),
                "z_max_um": self.z_max.value(),
                "stale_temp_s": self.stale.value(),
                "idle_timeout_min": self.idle_timeout.value(),
                "idle_grace_min": self.idle_grace.value(),
            },
            "keys": keys,
            "logging": {"dir": self.log_dir.text().strip(), "interval_s": self.log_interval.value()},
            "presets": base.presets,
        }
        return config_from_dict(data)

    def save(self) -> bool:
        try:
            cfg = self.to_config()
            path = self.c.save_config(cfg)
        except (ConfigError, OSError) as exc:
            QMessageBox.warning(self, "Settings", f"Not saved:\n{exc}")
            return False
        self.saved.emit(cfg)
        self.message.emit(f"Settings saved to {path}")
        return True
