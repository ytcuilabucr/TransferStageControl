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
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "TourBox GUI",
        "Kinesis GUI",
        "Settings",
        "Profile",
        "Autotune",
        "Config",
        "Log",
    ]
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


def test_ramp_off_steps_setpoint_to_target(gui, qtbot):
    window, controller, _ = gui
    tab = window.temperature
    assert tab.ramp_btn.isChecked() and tab.rate_spin.isEnabled()
    tab.ramp_btn.click()  # Ramp: Off
    assert not tab.ramp_btn.isChecked() and not tab.rate_spin.isEnabled()
    assert tab.ramp_btn.text() == "Ramp: Off"
    tab.rate_spin.setValue(10.0)  # ignored while ramp is off
    tab.sp_spin.setValue(80.0)
    tab.set_btn.click()
    qtbot.waitUntil(
        lambda: controller.last_thermal.target_sp_c == pytest.approx(80.0)
        and controller.last_thermal.program_sp_c == pytest.approx(80.0),
        timeout=3000,
    )
    assert not controller.last_thermal.ramping


def test_autotune_panel_start_cancel_and_apply(gui, qtbot):
    from transferstage.control.autotune import (
        RECOMMENDED_RULE,
        AutotuneResult,
        AutotuneState,
        AutotuneStatus,
        gains_from_ultimate,
    )

    window, controller, _ = gui
    tab = window.temperature
    tab.psu_btn.click()
    qtbot.waitUntil(lambda: controller.last_thermal.psu_on, timeout=3000)
    tab.at_sp.setValue(30.0)
    tab.at_bias.setValue(3.0)
    tab.at_amp.setValue(2.0)
    tab.at_start_btn.click()
    qtbot.waitUntil(lambda: controller.last_thermal.mode == "autotune", timeout=3000)
    qtbot.waitUntil(lambda: tab.at_cancel_btn.isEnabled(), timeout=3000)
    assert not tab.at_start_btn.isEnabled() and "Autotune:" in tab.at_status.text()
    assert controller.last_thermal.v_cmd in (pytest.approx(1.0), pytest.approx(5.0))

    tab.at_cancel_btn.click()
    qtbot.waitUntil(lambda: "aborted" in tab.at_status.text(), timeout=3000)
    assert tab.at_start_btn.isEnabled() and not tab.at_apply_btn.isEnabled()
    qtbot.waitUntil(lambda: controller.last_thermal.mode == "pid", timeout=3000)

    # A finished run fills the table; Apply gains copies the recommended row into the PID.
    result = AutotuneResult(2.0, 60.0, 1.0, 2.0, 0.05, gains_from_ultimate(2.0, 60.0))
    tab.on_autotune_finished(AutotuneStatus(AutotuneState.DONE, 3, 3, 600.0, 0.0, "ok", result))
    assert tab.at_apply_btn.isEnabled() and tab.at_progress.value() == 100
    tab.at_apply_btn.click()
    g = result.gains[RECOMMENDED_RULE]
    assert tab.kc_spin.value() == pytest.approx(g.kc, abs=1e-4)
    assert tab.ti_spin.value() == pytest.approx(g.ti_min, abs=1e-4)
    qtbot.waitUntil(lambda: controller.thermal.pid.kc == pytest.approx(g.kc), timeout=3000)
    assert controller.thermal.pid.ti_min == pytest.approx(g.ti_min)


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


def test_jog_tourbox_tap_pair_moves_until_second_tap(gui, qtbot):
    """TourBox sends an instant tap on button-down and another on button-up."""
    window, controller, statuses = gui
    z = controller.devices.z
    imap = window.input_map
    now = [100.0]
    imap._clock = lambda: now[0]
    start = z.position_um

    # button down: press + release 2 ms apart
    _key(window, QEvent.KeyPress, Qt.Key_F2)
    now[0] += 0.002
    _key(window, QEvent.KeyRelease, Qt.Key_F2)
    qtbot.waitUntil(lambda: z.position_um > start + 20, timeout=3000)
    assert imap.jogging and z.state is MotorState.MOVING
    # the GUI ▲ button mirrors the held Tall button
    assert window.manual.up_btn.isDown() and not window.manual.down_btn.isDown()

    # button up: the second tap stops the jog
    now[0] += 1.0
    _key(window, QEvent.KeyPress, Qt.Key_F2)
    now[0] += 0.002
    _key(window, QEvent.KeyRelease, Qt.Key_F2)
    qtbot.waitUntil(lambda: z.state is MotorState.IDLE, timeout=3000)
    assert not imap.jogging
    assert not window.manual.up_btn.isDown()

    # the next button-down tap starts a new jog (state was reset)
    stopped = z.position_um
    _key(window, QEvent.KeyPress, Qt.Key_F3)
    now[0] += 0.002
    _key(window, QEvent.KeyRelease, Qt.Key_F3)
    qtbot.waitUntil(lambda: z.position_um < stopped - 20, timeout=3000)
    assert window.manual.down_btn.isDown() and not window.manual.up_btn.isDown()
    _key(window, QEvent.KeyPress, Qt.Key_F3)
    _key(window, QEvent.KeyRelease, Qt.Key_F3)
    qtbot.waitUntil(lambda: z.state is MotorState.IDLE, timeout=3000)
    assert not imap.jogging


def test_jog_tap_stopped_by_focus_loss(gui, qtbot):
    window, controller, _ = gui
    imap = window.input_map
    now = [100.0]
    imap._clock = lambda: now[0]
    _key(window, QEvent.KeyPress, Qt.Key_F2)
    _key(window, QEvent.KeyRelease, Qt.Key_F2)
    assert imap.jogging
    assert window.manual.up_btn.isDown()
    imap.eventFilter(window, QEvent(QEvent.ApplicationDeactivate))
    assert not imap.jogging
    assert not window.manual.up_btn.isDown()
    qtbot.waitUntil(lambda: controller.devices.z.state is MotorState.IDLE, timeout=3000)


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
    m = window.manual
    m.speed_knob.setValue(2)
    _key(window, QEvent.KeyPress, Qt.Key_F4)
    assert m.speed_index == 3
    target = controller.config.motion.speed_steps_um_s[3]
    assert m.selected_speed() == pytest.approx(target)
    qtbot.waitUntil(lambda: z.velocity_um_s == pytest.approx(target), timeout=3000)
    _key(window, QEvent.KeyPress, Qt.Key_F5)
    _key(window, QEvent.KeyPress, Qt.Key_F5)
    assert m.speed_index == 1


def test_speed_settings_saved(gui, qtbot, tmp_path):
    window, controller, _ = gui
    m = window.manual
    m.speed_steps[4].setValue(300.0)
    m._on_speed_steps_edited()
    assert load_config(tmp_path / "config.yaml").motion.speed_steps_um_s[4] == pytest.approx(300.0)


def test_stop_button_cancels_and_heater_off(gui, qtbot):
    window, controller, _ = gui
    window.temperature.psu_btn.click()
    qtbot.waitUntil(lambda: controller.devices.psu.is_on, timeout=3000)
    window.stop_btn.click()
    qtbot.waitUntil(lambda: not controller.last_thermal.psu_on, timeout=3000)


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


def test_mark_and_action_log(gui, qtbot, caplog):
    import csv
    import logging

    import yaml

    window, controller, _ = gui
    caplog.set_level(logging.INFO)
    meta = yaml.safe_load(controller.datalog.meta_path.read_text(encoding="utf-8"))
    assert meta["connect_errors"] == {} and meta["config"]["pid"]["period_ms"] == 100

    window.temperature.psu_btn.click()
    window.note_edit.setText("stamp contact")
    window.mark_btn.click()
    assert window.note_edit.text() == ""
    _key(window, QEvent.KeyPress, Qt.Key_F7)  # mark shortcut, empty note
    _key(window, QEvent.KeyRelease, Qt.Key_F7)

    with controller.datalog.path.open(newline="", encoding="utf-8") as fh:
        notes = [r["note"] for r in csv.DictReader(fh) if r["note"]]
    assert notes == ["stamp contact", "mark 2"]
    text = caplog.text
    assert "PSU output on" in text and "MARK: stamp contact" in text and "MARK: mark 2" in text


def test_close_runs_safe_shutdown(gui, qtbot):
    window, controller, _ = gui
    window.temperature.psu_btn.click()
    qtbot.waitUntil(lambda: controller.devices.psu.is_on, timeout=3000)
    window.close()
    assert not controller.devices.psu.is_on
    assert not controller.devices.z.is_connected
    assert controller.datalog is not None and not controller.datalog.is_open


def test_check_connection_reports_and_reconnects(gui, qtbot):
    window, controller, _ = gui
    m = window.manual
    results = []
    controller.motion.connection.connect(results.append)

    m.check_conn_btn.click()
    qtbot.waitUntil(lambda: len(results) == 1, timeout=3000)
    assert results[0]["z"][0] and results[0]["rotator"][0]
    assert m.check_conn_btn.isEnabled()
    assert m.conn_details["z"].text().startswith("responding")

    # Z stops answering and cannot be reconnected
    controller.devices.z.fault = RuntimeError("USB unplugged")
    m.check_conn_btn.click()
    qtbot.waitUntil(lambda: len(results) == 2, timeout=3000)
    assert results[1]["z"] == (False, "not connected: USB unplugged")
    assert results[1]["rotator"][0]
    assert "USB unplugged" in str(controller.connect_errors["z"])
    assert not controller.device_connected("z")

    # cable back in: the next check reconnects it
    controller.devices.z.fault = None
    m.check_conn_btn.click()
    qtbot.waitUntil(lambda: len(results) == 3, timeout=3000)
    assert results[2]["z"][0] and results[2]["z"][1].startswith("connected; responding")
    assert controller.device_connected("z")
    assert "z" not in controller.connect_errors


def test_idle_watchdog_on_by_default_and_off_needs_confirmation(gui, monkeypatch):
    window, controller, _ = gui
    assert window.watchdog_chk.isChecked() and window.watchdog.enabled
    answers = [False]
    monkeypatch.setattr(window, "_confirm_disable_watchdog", lambda: answers.pop(0))
    window.watchdog_chk.click()  # declined
    assert window.watchdog_chk.isChecked() and window.watchdog.enabled
    answers.append(True)
    window.watchdog_chk.click()  # confirmed
    assert not window.watchdog_chk.isChecked() and not window.watchdog.enabled
    window.watchdog_chk.click()  # back on, no confirmation needed
    assert window.watchdog.enabled


def test_idle_alarm_unanswered_switches_heater_off(gui, qtbot):
    window, controller, _ = gui
    wd = window.watchdog
    wd.set_times(1.0, 1.0)
    wd._system_idle = lambda: 999.0  # nobody touches the PC
    wd._last_app_input = -1e9
    window.temperature.psu_btn.click()
    qtbot.waitUntil(lambda: controller.last_thermal.psu_on, timeout=3000)
    qtbot.waitUntil(window.idle_dialog.isVisible, timeout=5000)
    assert window.idle_dialog.here_btn.isVisible()
    qtbot.waitUntil(lambda: not controller.last_thermal.psu_on, timeout=5000)
    assert window.idle_dialog.isVisible() and window.idle_dialog.ok_btn.isVisible()
    assert not controller.devices.psu.is_on
    window.idle_dialog.ok_btn.click()
    assert not window.idle_dialog.isVisible()


def test_idle_alarm_answered_keeps_heater_on(gui, qtbot):
    window, controller, _ = gui
    wd = window.watchdog
    wd.set_times(1.0, 30.0)
    wd._system_idle = lambda: 999.0
    wd._last_app_input = -1e9
    window.temperature.psu_btn.click()
    qtbot.waitUntil(window.idle_dialog.isVisible, timeout=5000)
    window.idle_dialog.here_btn.click()
    assert not window.idle_dialog.isVisible()
    assert controller.last_thermal.psu_on
