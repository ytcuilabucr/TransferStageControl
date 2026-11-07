"""Settings tab: edit and save config.yaml.

Key bindings apply immediately on Save; everything else (devices, simulate
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
    "safe_lock": "TourBox Side + Top",
    "cancel_motion": "TourBox Top",
    "speed_up": "TourBox Knob clockwise",
    "speed_down": "TourBox Knob counter-clockwise",
    "jog_up": "TourBox Tall (hold)",
    "jog_down": "TourBox Short (hold)",
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
            QLabel("Key bindings apply on Save. Device, simulate, PID, limit and logging changes take effect on restart.")
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
        f.addRow("Kc:", self.kc)
        f.addRow("Ti:", self.ti)
        f.addRow("Td:", self.td)
        f.addRow("PID period:", self.period)
        f.addRow("Output low:", self.out_lo)
        f.addRow("Output high:", self.out_hi)
        f.addRow("Default ramp rate:", self.rate)
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
        f.addRow("Max temperature:", self.t_max)
        f.addRow("Max PSU voltage:", self.v_max)
        f.addRow("PSU current limit:", self.i_max)
        f.addRow("Z soft limit min:", self.z_min)
        f.addRow("Z soft limit max:", self.z_max)
        f.addRow("Stale temperature:", self.stale)
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
        self.rate.setValue(cfg.ramp.default_rate_deg_min)
        self.t_max.setValue(lim.t_max_c)
        self.i_max.setValue(lim.i_max)
        self.z_min.setValue(lim.z_min_um)
        self.z_max.setValue(lim.z_max_um)
        self.stale.setValue(lim.stale_temp_s)
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
            },
            "ramp": {"default_rate_deg_min": self.rate.value()},
            "motion": dataclasses.asdict(base.motion),
            "limits": {
                "t_max_c": min(self.t_max.value(), HARD_T_MAX_C),
                "v_max": min(self.v_max.value(), HARD_V_MAX),
                "i_max": self.i_max.value(),
                "z_min_um": self.z_min.value(),
                "z_max_um": self.z_max.value(),
                "stale_temp_s": self.stale.value(),
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
