"""Phase 5: GUI smoke tests (pytest-qt, simulators only)."""

import pytest
from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import QApplication

from transferstage.app import AppController
from transferstage.config import config_from_dict, load_config
from transferstage.drivers.base import MotorState
from transferstage.ui.input_map import parse_shortcut
from transferstage.ui.main_window import MainWindow

CA = Qt.ControlModifier


@pytest.fixture
def gui(qtbot, tmp_path):
    cfg = config_from_dict(
        {
            "pid": {"period_ms": 100},
            "limits": {"z_min_um": 0.0, "z_max_um": 20000.0},
            "logging": {"dir": str(tmp_path / "logs"), "interval_s": 0.2},
        }
    )
    controller = AppController(cfg, tmp_path / "config.yaml", sim_time_scale=20.0)
    window = MainWindow(controller, confirm_close=False)
    qtbot.addWidget(window)
    controller.start()
    window.show()
    statuses = []
    controller.motion.status.connect(statuses.append)
    qtbot.waitUntil(lambda: bool(statuses) and controller.last_thermal is not None, timeout=3000)
    yield window, controller, statuses
    window.close()
    controller.shutdown()


def _key(widget, kind, key, mods=CA, autorep=False):
    ev = QKeyEvent(kind, key, mods, "", autorep)
    QApplication.sendEvent(widget, ev)


def test_window_opens_with_sim_devices(gui):
    window, controller, statuses = gui
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == ["Manual", "Temperature", "Settings", "Log"]
    assert all(controller.device_connected(n) for n in ("z", "rotator", "psu", "thermometer"))
    assert statuses[-1].z_state is MotorState.IDLE
    assert window.temperature.ro["T"].text() != "—"
    assert "SIM" in window.sim_label.text()


def test_set_temperature_starts_ramping(gui, qtbot):
    window, controller, _ = gui
    tab = window.temperature
    tab.sp_spin.setValue(100.0)
    tab.rate_spin.setValue(10.0)
    tab.set_btn.click()
    qtbot.waitUntil(lambda: tab.ramping_led.is_on, timeout=3000)
    s = controller.last_thermal
    assert s.ramping and s.target_sp_c == pytest.approx(100.0)
    assert s.program_sp_c < 100.0

    tab.psu_btn.click()  # PSU on -> PID drives the heater
    qtbot.waitUntil(lambda: controller.last_thermal.psu_on and controller.last_thermal.v_cmd > 0, timeout=3000)
    assert tab.psu_btn.isChecked()


def test_jog_key_press_release_moves_and_stops(gui, qtbot):
    window, controller, statuses = gui
    z = controller.devices.z
    start = z.position_um
    _key(window, QEvent.KeyPress, Qt.Key_F2)
    qtbot.waitUntil(lambda: z.position_um > start + 20, timeout=3000)
    # auto-repeat press/release pairs while the key is held must not stop the jog
    _key(window, QEvent.KeyRelease, Qt.Key_F2, autorep=True)
    _key(window, QEvent.KeyPress, Qt.Key_F2, autorep=True)
    qtbot.wait(200)
    assert z.state is MotorState.MOVING
    # real release (modifiers may already be up) stops it
    _key(window, QEvent.KeyRelease, Qt.Key_F2, mods=Qt.NoModifier)
    qtbot.waitUntil(lambda: z.state is MotorState.IDLE, timeout=3000)
    stopped = z.position_um
    qtbot.wait(300)
    assert z.position_um == pytest.approx(stopped)
    assert not window.input_map.jogging


def test_safe_lock_blocks_jog(gui, qtbot):
    window, controller, _ = gui
    z = controller.devices.z
    _key(window, QEvent.KeyPress, Qt.Key_F1)
    _key(window, QEvent.KeyRelease, Qt.Key_F1)
    assert window.manual.safe_lock_btn.isChecked()
    assert not window.manual.up_btn.isEnabled()
    start = z.position_um
    _key(window, QEvent.KeyPress, Qt.Key_F2)
    qtbot.wait(500)
    _key(window, QEvent.KeyRelease, Qt.Key_F2)
    assert z.position_um == pytest.approx(start)
    assert z.state is MotorState.IDLE

    # unlock -> jog works again
    _key(window, QEvent.KeyPress, Qt.Key_F1)
    assert not window.manual.safe_lock_btn.isChecked()
    window.manual.up_btn.pressed.emit()
    qtbot.waitUntil(lambda: z.position_um > start + 20, timeout=3000)
    window.manual.up_btn.released.emit()
    qtbot.waitUntil(lambda: z.state is MotorState.IDLE, timeout=3000)


def test_speed_keys_change_speed(gui, qtbot):
    window, controller, _ = gui
    z = controller.devices.z
    before = window.manual.speed_spin.value()
    _key(window, QEvent.KeyPress, Qt.Key_F4)
    assert window.manual.speed_spin.value() > before
    target = window.manual.speed_spin.value()
    qtbot.waitUntil(lambda: z.velocity_um_s == pytest.approx(target), timeout=3000)


def test_presets_and_settings_save(gui, qtbot, tmp_path):
    window, controller, _ = gui
    window.manual.save_preset("home spot")
    on_disk = load_config(tmp_path / "config.yaml")
    assert [p["name"] for p in on_disk.presets] == ["home spot"]

    st = window.settings
    assert st.t_max.maximum() == 300.0 and st.v_max.maximum() == 24.0
    st.key_edits["cancel_motion"].setKeySequence(parse_shortcut("Ctrl+F9"))
    assert st.save()
    on_disk = load_config(tmp_path / "config.yaml")
    assert on_disk.keys["cancel_motion"] == "Ctrl+F9"
    assert on_disk.presets  # presets survive a settings save
    assert window.input_map._combos[parse_shortcut("Ctrl+F9")] == "cancel_motion"


def test_close_runs_safe_shutdown(gui, qtbot):
    window, controller, _ = gui
    window.temperature.psu_btn.click()
    qtbot.waitUntil(lambda: controller.devices.psu.is_on, timeout=3000)
    window.close()
    assert not controller.devices.psu.is_on
    assert not controller.devices.z.is_connected
    assert controller.datalog is not None and not controller.datalog.is_open
