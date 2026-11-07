"""Motion panels, laid out like the LabVIEW front panel (gui_v2.vi).

``ManualPanel`` owns the widgets for the Z stage and the rotator and hands them
to the main window as separate pieces:

* ``z_panel``        – left column "Z-Controller": status, set/current speed, position slide
* ``rotator_panel``  – left column "Sample Rotator": current angle, set angle
* ``tourbox_page``   – centre tab "TourBox GUI": Safe Lock, Cancel, speed settings + knob, jog
* ``kinesis_page``   – centre tab "Kinesis GUI": move to / homing / position presets
"""

from __future__ import annotations

import logging

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QDial,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..app import AppController
from ..control.motion import MotionStatus
from ..drivers.base import MotorState
from .widgets import AMBER, GREEN, LED_GREEN_OFF, RED, Led, VerticalGauge, fmt, indicator, section, spin

log = logging.getLogger(__name__)

N_SPEEDS = 5


def _since(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} s"
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class ManualPanel(QObject):
    message = pyqtSignal(str)  # short status-bar notices

    def __init__(self, controller: AppController, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.c = controller
        lim = controller.config.limits
        self.z_min, self.z_max = lim.z_min_um, lim.z_max_um
        self._last_status: MotionStatus | None = None
        self._prev_z: float | None = None
        self._speed_applied = False

        self.z_panel = self._build_z_panel()
        self.rotator_panel = self._build_rotator_panel()
        self.tourbox_page = self._build_tourbox_page()
        self.kinesis_page = self._build_kinesis_page()

        self._load_presets()
        self._update_set_speed_label()

    # ------------------------------------------------------------------ left column

    def _build_z_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        self.z_status = indicator("—", 80)
        v.addWidget(section("Z-Controller", extra=self.z_status))

        grid = QGridLayout()
        self.set_speed_label = indicator("—")
        self.speed_label = indicator("—")
        self.z_gauge = VerticalGauge(self.z_min / 1000.0, self.z_max / 1000.0)
        self.z_gauge.setToolTip("Z position (mm) between the soft limits")
        self.z_label = indicator("—", 100)
        left = QVBoxLayout()
        left.addWidget(QLabel("Set Speed"))
        left.addWidget(self.set_speed_label)
        left.addSpacing(8)
        left.addWidget(QLabel("Current Speed"))
        left.addWidget(self.speed_label)
        left.addStretch(1)
        right = QVBoxLayout()
        right.addWidget(QLabel("Z-Position"))
        right.addWidget(self.z_label)
        right.addStretch(1)
        grid.addLayout(left, 0, 0)
        grid.addWidget(self.z_gauge, 0, 1)
        grid.addLayout(right, 0, 2)
        grid.setColumnStretch(2, 1)
        v.addLayout(grid)
        return w

    def _build_rotator_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        self.rot_status = QLabel("—")
        v.addWidget(section("Sample Rotator", extra=self.rot_status))
        grid = QGridLayout()
        self.angle_label = indicator("—")
        self.angle_spin = spin(-360, 360, 0, decimals=2, step=1, suffix=" °")
        self.set_angle_btn = QPushButton("✔ Set")
        self.set_angle_btn.clicked.connect(lambda: self.c.rotate_to.emit(self.angle_spin.value()))
        grid.addWidget(QLabel("Current Angle"), 0, 0, Qt.AlignRight)
        grid.addWidget(self.angle_label, 0, 1)
        grid.addWidget(QLabel("Set Angle"), 1, 0, Qt.AlignRight)
        grid.addWidget(self.angle_spin, 1, 1)
        grid.addWidget(self.set_angle_btn, 1, 2)
        grid.setColumnStretch(1, 1)
        v.addLayout(grid)
        return w

    # ------------------------------------------------------------------ TourBox GUI tab

    def _build_tourbox_page(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)

        top = QHBoxLayout()
        self.safe_lock_btn = QPushButton("🔒  Safe Lock")
        self.safe_lock_btn.setCheckable(True)
        self.safe_lock_btn.setMinimumSize(120, 38)
        self.safe_lock_btn.setStyleSheet(
            f"QPushButton:checked {{ background:{AMBER}; color:black; font-weight:bold; }}"
        )
        self.safe_lock_btn.toggled.connect(self._on_safe_lock_toggled)
        self.cancel_btn = QPushButton("✖  Cancel")
        self.cancel_btn.setMinimumSize(120, 38)
        self.cancel_btn.setStyleSheet(f"QPushButton {{ color:{RED}; font-weight:bold; }}")
        self.cancel_btn.clicked.connect(self.c.cancel_motion.emit)
        top.addSpacing(40)
        top.addWidget(self.safe_lock_btn)
        top.addWidget(self.cancel_btn)
        top.addStretch(1)
        root.addLayout(top)
        root.addSpacing(16)

        mid = QHBoxLayout()
        # speed settings column
        sv = QVBoxLayout()
        sv.addWidget(QLabel("Speed Settings\n(µm/s)"))
        box = QFrame()
        box.setFrameShape(QFrame.Box)
        bv = QVBoxLayout(box)
        bv.setContentsMargins(4, 4, 4, 4)
        bv.setSpacing(2)
        self.speed_steps = []
        for i, val in enumerate(self.c.config.motion.speed_steps_um_s):
            sp = spin(0.1, 1e5, val, decimals=1, step=1)
            sp.setButtonSymbols(sp.NoButtons)
            sp.setAlignment(Qt.AlignRight)
            sp.setMaximumWidth(80)
            sp.setToolTip(f"Speed for knob position {i}")
            sp.editingFinished.connect(self._on_speed_steps_edited)
            self.speed_steps.append(sp)
            bv.addWidget(sp)
        sv.addWidget(box)
        sv.addStretch(1)
        mid.addLayout(sv)

        # speed knob 0..4
        kv = QVBoxLayout()
        lab = QLabel("Speed Knob")
        lab.setAlignment(Qt.AlignHCenter)
        kv.addWidget(lab)
        self.speed_knob = QDial()
        self.speed_knob.setRange(0, N_SPEEDS - 1)
        self.speed_knob.setNotchesVisible(True)
        self.speed_knob.setWrapping(False)
        self.speed_knob.setPageStep(1)
        self.speed_knob.setFixedSize(140, 140)
        self.speed_knob.setValue(2)
        self.speed_knob.valueChanged.connect(self._on_knob)
        kv.addWidget(self.speed_knob, alignment=Qt.AlignHCenter)
        self.knob_pos_label = QLabel()
        self.knob_pos_label.setAlignment(Qt.AlignHCenter)
        kv.addWidget(self.knob_pos_label)
        kv.addStretch(1)
        mid.addLayout(kv, 1)
        root.addLayout(mid)

        # jog buttons (press-and-hold) with LEDs
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        big = QFont()
        big.setPointSize(20)
        self.up_btn = QPushButton("▲")
        self.down_btn = QPushButton("▼")
        self.up_led = Led("", GREEN, off_color=LED_GREEN_OFF)
        self.down_led = Led("", GREEN, off_color=LED_GREEN_OFF)
        for b, led, tip in ((self.up_btn, self.up_led, "Jog up (hold)"), (self.down_btn, self.down_led, "Jog down (hold)")):
            b.setFont(big)
            b.setFixedSize(52, 76)
            b.setAutoRepeat(False)
            b.setToolTip(tip)
            col = QVBoxLayout()
            col.addWidget(b)
            col.addWidget(led, alignment=Qt.AlignHCenter)
            bottom.addLayout(col)
        self.up_btn.setStyleSheet("QPushButton { color:#3cb371; }")
        self.down_btn.setStyleSheet("QPushButton { color:#3cb371; }")
        self.up_btn.pressed.connect(lambda: self.c.jog_start.emit(1))
        self.down_btn.pressed.connect(lambda: self.c.jog_start.emit(-1))
        self.up_btn.released.connect(self.c.jog_stop.emit)
        self.down_btn.released.connect(self.c.jog_stop.emit)
        bottom.addSpacing(10)
        root.addStretch(1)
        root.addLayout(bottom)

        since = QHBoxLayout()
        since.addStretch(1)
        self.busy_led = Led("Busy", AMBER)
        since.addWidget(self.busy_led)
        since.addSpacing(16)
        since.addWidget(QLabel("Since Last Motion"))
        self.since_label = indicator("—", 70)
        since.addWidget(self.since_label)
        root.addLayout(since)
        return w

    # ------------------------------------------------------------------ Kinesis GUI tab

    def _build_kinesis_page(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        zg = QGroupBox(f"Z stage   (soft limits {self.z_min / 1000:g} – {self.z_max / 1000:g} mm)")
        zgrid = QGridLayout(zg)
        self.move_spin = spin(self.z_min, self.z_max, self.z_min, decimals=1, step=10, suffix=" µm")
        self.move_btn = QPushButton("Move Z to")
        self.move_btn.clicked.connect(lambda: self.c.move_z_to.emit(self.move_spin.value()))
        self.home_z_btn = QPushButton("Home Z")
        self.home_z_btn.clicked.connect(self._home_z)
        zgrid.addWidget(self.move_btn, 0, 0)
        zgrid.addWidget(self.move_spin, 0, 1)
        zgrid.addWidget(self.home_z_btn, 0, 2)
        zgrid.setColumnStretch(1, 1)
        v.addWidget(zg)

        rg = QGroupBox("Rotator")
        rh = QHBoxLayout(rg)
        self.home_rot_btn = QPushButton("Home Rotator")
        self.home_rot_btn.clicked.connect(self._home_rotator)
        rh.addWidget(self.home_rot_btn)
        rh.addStretch(1)
        v.addWidget(rg)

        pg_ = QGroupBox("Position presets (Z + angle)")
        pv = QVBoxLayout(pg_)
        self.preset_list = QListWidget()
        self.preset_list.itemDoubleClicked.connect(lambda _: self.goto_preset())
        pv.addWidget(self.preset_list, 1)
        h = QHBoxLayout()
        self.save_preset_btn = QPushButton("Save current…")
        self.goto_preset_btn = QPushButton("Go to")
        self.delete_preset_btn = QPushButton("Delete")
        self.save_preset_btn.clicked.connect(self.save_preset)
        self.goto_preset_btn.clicked.connect(self.goto_preset)
        self.delete_preset_btn.clicked.connect(self.delete_preset)
        for b in (self.save_preset_btn, self.goto_preset_btn, self.delete_preset_btn):
            h.addWidget(b)
        pv.addLayout(h)
        v.addWidget(pg_, 1)
        return w

    # ------------------------------------------------------------------ speed

    @property
    def speed_index(self) -> int:
        return int(self.speed_knob.value())

    def selected_speed(self) -> float:
        return float(self.speed_steps[self.speed_index].value())

    def _update_set_speed_label(self) -> None:
        self.set_speed_label.setText(f"{self.selected_speed():g} µm/s")
        self.knob_pos_label.setText(f"position {self.speed_index}: {self.selected_speed():g} µm/s")
        for i, sp in enumerate(self.speed_steps):
            sp.setStyleSheet("QDoubleSpinBox { background:#fff3c4; font-weight:bold; }" if i == self.speed_index else "")

    def _on_knob(self, _index: int) -> None:
        self._update_set_speed_label()
        self.apply_speed()

    def _on_speed_steps_edited(self) -> None:
        steps = [float(sp.value()) for sp in self.speed_steps]
        if steps == list(self.c.config.motion.speed_steps_um_s):
            return
        try:
            self.c.save_speed_steps(steps)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self.tourbox_page, "Speed settings", f"Could not save speed settings:\n{exc}")
            return
        self._update_set_speed_label()
        self.apply_speed()

    def apply_speed(self) -> None:
        self.c.set_speed.emit(self.selected_speed())

    def show_key_jog(self, direction: int) -> None:
        """Mirror a TourBox Tall/Short hold on the ▲/▼ buttons (+1 up, -1 down, 0 released).

        ``setDown`` only changes the look; it does not emit pressed/released, so no jog is re-sent.
        """
        self.up_btn.setDown(direction > 0)
        self.down_btn.setDown(direction < 0)

    def nudge_speed(self, sign: int) -> None:
        """TourBox knob: move the Speed Knob one position and apply that speed."""
        self.speed_knob.setValue(min(max(self.speed_index + (1 if sign > 0 else -1), 0), N_SPEEDS - 1))

    # ------------------------------------------------------------------ safe lock / homing

    def _on_safe_lock_toggled(self, on: bool) -> None:
        self.c.set_safe_lock.emit(on)
        for w in (
            self.up_btn,
            self.down_btn,
            self.speed_knob,
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
            QMessageBox.question(
                self.kinesis_page, "Home Z", "Home the Z stage? It will move to its hardware home position."
            )
            == QMessageBox.Yes
        ):
            self.c.home_z.emit()

    def _home_rotator(self) -> None:
        if QMessageBox.question(self.kinesis_page, "Home rotator", "Home the rotator?") == QMessageBox.Yes:
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
            QMessageBox.warning(self.kinesis_page, "Presets", f"Could not save presets:\n{exc}")
            return False
        self._load_presets()
        return True

    def save_preset(self, name: str | None = None) -> None:
        st = self._last_status
        if st is None or st.z_um is None or st.rotator_deg is None:
            QMessageBox.information(self.kinesis_page, "Presets", "Position not known yet.")
            return
        if name is None:
            name, ok = QInputDialog.getText(self.kinesis_page, "Save preset", "Preset name:")
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
        if QMessageBox.question(self.kinesis_page, "Delete preset", f"Delete preset '{name}'?") != QMessageBox.Yes:
            return
        self._store([p for p in self._presets() if p["name"] != name])

    # ------------------------------------------------------------------ status

    def on_status(self, st: MotionStatus) -> None:
        self._last_status = st
        if not self._speed_applied and st.z_state is not MotorState.DISCONNECTED:
            self._speed_applied = True
            self.apply_speed()  # knob position at start-up = the speed in use

        z = st.z_um
        self.z_label.setText(fmt(None if z is None else z / 1000.0, 4, "mm"))
        self.z_gauge.set_value(None if z is None else z / 1000.0)
        self.z_status.setText(st.z_state.value.capitalize())
        color = {MotorState.IDLE: "#111", MotorState.ERROR: RED, MotorState.DISCONNECTED: RED}.get(st.z_state, "#b36b00")
        self.z_status.setStyleSheet(f"color:{color};")
        self.since_label.setText(_since(st.seconds_since_last_motion))
        self.speed_label.setText(fmt(st.speed_um_s, 0, "µm/s"))

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
