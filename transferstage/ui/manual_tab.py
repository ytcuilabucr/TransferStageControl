"""Manual (TourBox) tab: Z jog/speed/position, rotator angle, presets, homing."""

from __future__ import annotations

import logging

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QDial,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..app import AppController
from ..control.motion import MotionStatus
from ..drivers.base import MotorState
from .widgets import AMBER, BLUE, GREEN, RED, Led, fmt, spin

log = logging.getLogger(__name__)

SLIDER_STEPS = 1000


def _since(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class ManualTab(QWidget):
    message = pyqtSignal(str)  # short status-bar notices

    SPEED_STEP_FRACTION = 0.05  # speed_up / speed_down change the speed by 5 % of max velocity

    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.c = controller
        lim = controller.config.limits
        self.z_min, self.z_max = lim.z_min_um, lim.z_max_um
        self._max_speed: float | None = None
        self._last_status: MotionStatus | None = None
        self._prev_z: float | None = None

        root = QHBoxLayout(self)
        root.addWidget(self._build_z_group(), 3)
        right = QVBoxLayout()
        right.addWidget(self._build_rotator_group())
        right.addWidget(self._build_presets_group(), 1)
        root.addLayout(right, 2)

        self._load_presets()
        self._init_max_speed()

    # ------------------------------------------------------------------ layout

    def _build_z_group(self) -> QGroupBox:
        g = QGroupBox("Z stage")
        grid = QGridLayout(g)

        big = QFont()
        big.setPointSize(12)
        big.setBold(True)

        # jog buttons (press-and-hold)
        self.up_btn = QPushButton("▲  Up")
        self.down_btn = QPushButton("▼  Down")
        for b in (self.up_btn, self.down_btn):
            b.setFont(big)
            b.setMinimumHeight(56)
            b.setAutoRepeat(False)
        self.up_btn.pressed.connect(lambda: self.c.jog_start.emit(1))
        self.down_btn.pressed.connect(lambda: self.c.jog_start.emit(-1))
        self.up_btn.released.connect(self.c.jog_stop.emit)
        self.down_btn.released.connect(self.c.jog_stop.emit)

        jog = QVBoxLayout()
        jog.addWidget(self.up_btn)
        jog.addWidget(self.down_btn)
        ind = QHBoxLayout()
        self.up_led = Led("Up", BLUE)
        self.down_led = Led("Down", BLUE)
        self.busy_led = Led("Busy", AMBER)
        for w in (self.up_led, self.down_led, self.busy_led):
            ind.addWidget(w)
        jog.addLayout(ind)

        # cancel + safe lock
        self.cancel_btn = QPushButton("Cancel Motion")
        self.cancel_btn.setMinimumHeight(40)
        self.cancel_btn.setStyleSheet(f"QPushButton {{ background:{RED}; color:white; font-weight:bold; }}")
        self.cancel_btn.clicked.connect(self.c.cancel_motion.emit)
        self.safe_lock_btn = QPushButton("Safe Lock")
        self.safe_lock_btn.setCheckable(True)
        self.safe_lock_btn.setMinimumHeight(40)
        self.safe_lock_btn.setStyleSheet(
            f"QPushButton:checked {{ background:{AMBER}; color:black; font-weight:bold; }}"
        )
        self.safe_lock_btn.toggled.connect(self._on_safe_lock_toggled)
        jog.addWidget(self.cancel_btn)
        jog.addWidget(self.safe_lock_btn)
        jog.addStretch(1)
        grid.addLayout(jog, 0, 0, 2, 1)

        # speed knob
        sp = QVBoxLayout()
        sp.addWidget(QLabel("Speed (µm/s)"), alignment=Qt.AlignHCenter)
        self.speed_dial = QDial()
        self.speed_dial.setNotchesVisible(True)
        self.speed_dial.setWrapping(False)
        self.speed_dial.setMinimumSize(130, 130)
        self.speed_dial.setRange(1, 2600)
        self.speed_spin = spin(1, 2600, 500, decimals=0, step=10, suffix=" µm/s")
        self.speed_dial.valueChanged.connect(lambda v: self.speed_spin.setValue(v))
        self.speed_spin.valueChanged.connect(lambda v: self.speed_dial.setValue(int(round(v))))
        self.set_speed_btn = QPushButton("Set Speed")
        self.set_speed_btn.clicked.connect(self.apply_speed)
        self.speed_label = QLabel("Current: —")
        sp.addWidget(self.speed_dial, alignment=Qt.AlignHCenter)
        sp.addWidget(self.speed_spin)
        sp.addWidget(self.set_speed_btn)
        sp.addWidget(self.speed_label)
        sp.addStretch(1)
        grid.addLayout(sp, 0, 1, 2, 1)

        # position slide + readouts
        pos = QVBoxLayout()
        pos.addWidget(QLabel(f"Z position\n{self.z_max:g} µm"), alignment=Qt.AlignHCenter)
        self.z_slider = QSlider(Qt.Vertical)
        self.z_slider.setRange(0, SLIDER_STEPS)
        self.z_slider.setEnabled(False)  # indicator only
        self.z_slider.setMinimumHeight(220)
        pos.addWidget(self.z_slider, 1, alignment=Qt.AlignHCenter)
        pos.addWidget(QLabel(f"{self.z_min:g} µm"), alignment=Qt.AlignHCenter)
        grid.addLayout(pos, 0, 2, 2, 1)

        info = QGridLayout()
        mono = QFont("Consolas")
        mono.setPointSize(14)
        self.z_label = QLabel("—")
        self.z_label.setFont(mono)
        self.z_status = QLabel("—")
        self.z_status.setFont(big)
        self.since_label = QLabel("—")
        info.addWidget(QLabel("Z (µm):"), 0, 0)
        info.addWidget(self.z_label, 0, 1)
        info.addWidget(QLabel("Z-status:"), 1, 0)
        info.addWidget(self.z_status, 1, 1)
        info.addWidget(QLabel("Since last motion:"), 2, 0)
        info.addWidget(self.since_label, 2, 1)
        self.move_spin = spin(self.z_min, self.z_max, self.z_min, decimals=1, step=10, suffix=" µm")
        self.move_btn = QPushButton("Move Z to")
        self.move_btn.clicked.connect(lambda: self.c.move_z_to.emit(self.move_spin.value()))
        info.addWidget(self.move_btn, 3, 0)
        info.addWidget(self.move_spin, 3, 1)
        self.home_z_btn = QPushButton("Home Z")
        self.home_z_btn.clicked.connect(self._home_z)
        info.addWidget(self.home_z_btn, 4, 0, 1, 2)
        info.setRowStretch(5, 1)
        grid.addLayout(info, 0, 3, 2, 1)
        grid.setColumnStretch(3, 1)
        return g

    def _build_rotator_group(self) -> QGroupBox:
        g = QGroupBox("Rotator")
        grid = QGridLayout(g)
        self.angle_spin = spin(-360, 360, 0, decimals=2, step=1, suffix=" °")
        self.set_angle_btn = QPushButton("Set Angle")
        self.set_angle_btn.clicked.connect(lambda: self.c.rotate_to.emit(self.angle_spin.value()))
        mono = QFont("Consolas")
        mono.setPointSize(14)
        self.angle_label = QLabel("—")
        self.angle_label.setFont(mono)
        self.rot_status = QLabel("—")
        self.home_rot_btn = QPushButton("Home Rotator")
        self.home_rot_btn.clicked.connect(self._home_rotator)
        grid.addWidget(QLabel("Set Angle:"), 0, 0)
        grid.addWidget(self.angle_spin, 0, 1)
        grid.addWidget(self.set_angle_btn, 0, 2)
        grid.addWidget(QLabel("Current Angle:"), 1, 0)
        grid.addWidget(self.angle_label, 1, 1)
        grid.addWidget(self.rot_status, 1, 2)
        grid.addWidget(self.home_rot_btn, 2, 0, 1, 3)
        return g

    def _build_presets_group(self) -> QGroupBox:
        g = QGroupBox("Position presets (Z + angle)")
        v = QVBoxLayout(g)
        self.preset_list = QListWidget()
        self.preset_list.itemDoubleClicked.connect(lambda _: self.goto_preset())
        v.addWidget(self.preset_list, 1)
        h = QHBoxLayout()
        self.save_preset_btn = QPushButton("Save current…")
        self.goto_preset_btn = QPushButton("Go to")
        self.delete_preset_btn = QPushButton("Delete")
        self.save_preset_btn.clicked.connect(self.save_preset)
        self.goto_preset_btn.clicked.connect(self.goto_preset)
        self.delete_preset_btn.clicked.connect(self.delete_preset)
        for b in (self.save_preset_btn, self.goto_preset_btn, self.delete_preset_btn):
            h.addWidget(b)
        v.addLayout(h)
        return g

    # ------------------------------------------------------------------ speed

    def _init_max_speed(self) -> None:
        try:
            mx = float(self.c.devices.z.max_velocity_um_s)
        except Exception:  # noqa: BLE001 - not connected yet; retried on the first status
            return
        if mx > 0:
            self._max_speed = mx
            self.speed_dial.setRange(1, int(mx))
            self.speed_spin.setRange(1, mx)

    def apply_speed(self) -> None:
        self.c.set_speed.emit(float(self.speed_spin.value()))

    def nudge_speed(self, sign: int) -> None:
        """TourBox knob: change the speed by a step and apply it immediately."""
        mx = self._max_speed or float(self.speed_spin.maximum())
        step = max(1.0, mx * self.SPEED_STEP_FRACTION)
        self.speed_spin.setValue(self.speed_spin.value() + sign * step)
        self.apply_speed()

    # ------------------------------------------------------------------ safe lock / homing

    def _on_safe_lock_toggled(self, on: bool) -> None:
        self.c.set_safe_lock.emit(on)
        for w in (
            self.up_btn,
            self.down_btn,
            self.set_speed_btn,
            self.move_btn,
            self.home_z_btn,
            self.set_angle_btn,
            self.home_rot_btn,
            self.goto_preset_btn,
        ):
            w.setEnabled(not on)

    def toggle_safe_lock(self) -> None:
        self.safe_lock_btn.toggle()

    def _home_z(self) -> None:
        if (
            QMessageBox.question(self, "Home Z", "Home the Z stage? It will move to its hardware home position.")
            == QMessageBox.Yes
        ):
            self.c.home_z.emit()

    def _home_rotator(self) -> None:
        if QMessageBox.question(self, "Home rotator", "Home the rotator?") == QMessageBox.Yes:
            self.c.home_rotator.emit()

    # ------------------------------------------------------------------ presets

    def _load_presets(self) -> None:
        self.preset_list.clear()
        for p in self.c.saved_config.presets:
            item = QListWidgetItem(f"{p['name']}   —   Z {p['z_um']:.1f} µm,  {p['angle_deg']:.2f}°")
            item.setData(Qt.UserRole, dict(p))
            self.preset_list.addItem(item)

    def _presets(self) -> list[dict]:
        return [self.preset_list.item(i).data(Qt.UserRole) for i in range(self.preset_list.count())]

    def _store(self, presets: list[dict]) -> bool:
        try:
            self.c.save_presets(presets)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Presets", f"Could not save presets:\n{exc}")
            return False
        self._load_presets()
        return True

    def save_preset(self, name: str | None = None) -> None:
        st = self._last_status
        if st is None or st.z_um is None or st.rotator_deg is None:
            QMessageBox.information(self, "Presets", "Position not known yet.")
            return
        if name is None:
            name, ok = QInputDialog.getText(self, "Save preset", "Preset name:")
            if not ok:
                return
        name = name.strip()
        if not name:
            return
        presets = [p for p in self._presets() if p["name"] != name]
        presets.append({"name": name, "z_um": round(st.z_um, 2), "angle_deg": round(st.rotator_deg, 3)})
        if self._store(presets):
            self.message.emit(f"Preset '{name}' saved")

    def goto_preset(self) -> None:
        item = self.preset_list.currentItem()
        if item is None:
            return
        p = item.data(Qt.UserRole)
        self.c.move_z_to.emit(float(p["z_um"]))
        self.c.rotate_to.emit(float(p["angle_deg"]))
        self.message.emit(f"Going to preset '{p['name']}'")

    def delete_preset(self) -> None:
        item = self.preset_list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)["name"]
        if QMessageBox.question(self, "Delete preset", f"Delete preset '{name}'?") != QMessageBox.Yes:
            return
        self._store([p for p in self._presets() if p["name"] != name])

    # ------------------------------------------------------------------ status

    def on_status(self, st: MotionStatus) -> None:
        self._last_status = st
        if self._max_speed is None:
            self._init_max_speed()

        z = st.z_um
        self.z_label.setText(fmt(z, 2))
        if z is not None:
            frac = (z - self.z_min) / (self.z_max - self.z_min)
            self.z_slider.setValue(int(round(min(max(frac, 0.0), 1.0) * SLIDER_STEPS)))
        self.z_status.setText(st.z_state.value)
        color = {MotorState.IDLE: GREEN, MotorState.ERROR: RED, MotorState.DISCONNECTED: RED}.get(st.z_state, AMBER)
        self.z_status.setStyleSheet(f"color:{color};")
        self.since_label.setText(_since(st.seconds_since_last_motion))
        self.speed_label.setText(f"Current: {fmt(st.speed_um_s, 0, 'µm/s')}")

        moving_up = moving_down = False
        if st.jog_direction:
            moving_up, moving_down = st.jog_direction > 0, st.jog_direction < 0
        elif st.z_state.is_busy and z is not None and self._prev_z is not None:
            moving_up, moving_down = z > self._prev_z + 1e-6, z < self._prev_z - 1e-6
        self._prev_z = z
        self.up_led.set_on(moving_up)
        self.down_led.set_on(moving_down)
        self.busy_led.set_on(st.busy)

        self.angle_label.setText(fmt(st.rotator_deg, 2, "°"))
        self.rot_status.setText(st.rotator_state.value)
        # The Safe Lock button is the source of truth; the worker's flag is not fed back
        # (a status queued before the toggle reached the worker would otherwise undo it).
