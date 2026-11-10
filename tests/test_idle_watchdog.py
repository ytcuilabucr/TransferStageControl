"""Idle-heater watchdog: alarm after idle time with the heater on, heater off if unanswered."""

import pytest

from transferstage.config import ConfigError, config_from_dict
from transferstage.ui.idle_watchdog import IdleWatchdog, WatchState, system_idle_seconds


class Rig:
    def __init__(self, qtbot, timeout_s=1200.0, grace_s=180.0):
        self.t = 1000.0
        self.heater = False
        self.sys_idle = None  # None = system idle time unknown; only app input + clock count
        self.events = []
        self.w = IdleWatchdog(
            lambda: self.heater,
            timeout_s=timeout_s,
            grace_s=grace_s,
            system_idle=lambda: self.sys_idle,
            clock=lambda: self.t,
        )
        self.w.warning_started.connect(lambda g: self.events.append(("warn", g)))
        self.w.warning_cleared.connect(lambda r: self.events.append(("clear", r)))
        self.w.heater_off.connect(lambda: self.events.append(("off",)))

    def run(self, seconds, step=1.0):
        end = self.t + seconds
        while self.t < end:
            self.t += step
            self.w.check()

    def kinds(self):
        return [e[0] for e in self.events]


@pytest.fixture
def rig(qtbot):
    return Rig(qtbot)


def test_no_alarm_while_heater_off(rig):
    rig.run(3600)
    assert rig.events == []
    assert rig.w.state is WatchState.OFF


def test_alarm_after_timeout_then_heater_off_after_grace(rig):
    rig.heater = True
    rig.run(1199)
    assert rig.events == []
    rig.run(2)
    assert rig.events == [("warn", 180.0)]
    assert rig.w.state is WatchState.WARNING
    rig.run(178)
    assert rig.kinds() == ["warn"]
    rig.run(3)
    assert rig.kinds() == ["warn", "off"]
    assert rig.w.state is WatchState.SHUT_OFF


def test_answering_restarts_the_idle_time(rig):
    rig.heater = True
    rig.run(1201)
    rig.w.acknowledge()
    assert rig.kinds() == ["warn", "clear"]
    assert rig.w.state is WatchState.WATCHING
    rig.run(1100)
    assert rig.kinds() == ["warn", "clear"]
    rig.run(200)
    assert rig.kinds() == ["warn", "clear", "warn"]


def test_input_in_any_program_counts_as_use(rig):
    rig.heater = True
    rig.sys_idle = 5.0  # someone keeps using the PC (another window)
    rig.run(3600)
    assert rig.events == []
    rig.sys_idle = 99999.0  # stopped; the watch started when the heater went on
    rig.run(1)
    assert rig.kinds() == ["warn"]


def test_input_during_countdown_does_not_cancel_it(rig):
    rig.heater = True
    rig.run(1201)
    rig.sys_idle = 0.0  # mouse bumped, nobody answered
    rig.run(180)
    assert rig.kinds() == ["warn", "off"]


def test_heater_off_elsewhere_clears_the_alarm(rig):
    rig.heater = True
    rig.run(1201)
    rig.heater = False
    rig.run(1)
    assert rig.kinds() == ["warn", "clear"]
    assert rig.w.state is WatchState.OFF
    rig.run(1000)
    assert rig.kinds() == ["warn", "clear"]


def test_heater_on_starts_a_fresh_watch(rig):
    rig.run(5000)  # long idle with the heater off
    rig.heater = True
    rig.run(10)
    assert rig.events == []


def test_shutoff_retries_until_heater_reports_off_then_rearms(rig):
    rig.heater = True
    rig.run(1381)
    assert rig.kinds() == ["warn", "off"]
    rig.run(6)  # heater still reported on -> off command again
    assert rig.kinds() == ["warn", "off", "off"]
    rig.heater = False
    rig.run(1)
    assert rig.w.state is WatchState.OFF
    rig.heater = True  # switched on again later: a new watch
    rig.run(1201)
    assert rig.kinds() == ["warn", "off", "off", "warn"]


def test_disabled_watchdog_never_alarms_and_disabling_clears_alarm(rig):
    rig.heater = True
    rig.run(1201)
    rig.w.set_enabled(False)
    assert rig.kinds() == ["warn", "clear"]
    rig.run(7200)
    assert rig.kinds() == ["warn", "clear"]
    rig.w.set_enabled(True)
    rig.run(1199)
    assert rig.kinds() == ["warn", "clear"]
    rig.run(2)
    assert rig.kinds() == ["warn", "clear", "warn"]


def test_system_idle_seconds_is_a_number_on_windows():
    v = system_idle_seconds()
    assert v is None or v >= 0


def test_config_defaults_and_validation():
    cfg = config_from_dict({})
    assert cfg.limits.idle_timeout_min == 20
    assert cfg.limits.idle_grace_min == 3
    with pytest.raises(ConfigError, match="idle_timeout_min"):
        config_from_dict({"limits": {"idle_timeout_min": 0}})
    with pytest.raises(ConfigError, match="idle_grace_min"):
        config_from_dict({"limits": {"idle_grace_min": -1}})
