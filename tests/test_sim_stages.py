import threading
import time

import pytest

from transferstage.drivers import Direction, MotorState, NotConnectedError
from transferstage.drivers.sim import SimLinearStage, SimRotaryStage


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def z(clock):
    stage = SimLinearStage(max_velocity_um_s=2000, velocity_um_s=1000, stop_decel_s=0.2, clock=clock)
    stage.connect()
    return stage


def test_disconnected_stage_rejects_commands():
    stage = SimLinearStage()
    assert stage.state is MotorState.DISCONNECTED
    with pytest.raises(NotConnectedError):
        stage.move_to(100)
    stage.connect()
    assert stage.state is MotorState.IDLE


def test_move_to_reaches_target_and_returns_idle(z, clock):
    z.move_to(500)
    assert z.state is MotorState.MOVING
    clock.advance(0.25)
    assert z.position_um == pytest.approx(250)
    assert z.state is MotorState.MOVING
    clock.advance(0.3)
    assert z.position_um == 500
    assert z.state is MotorState.IDLE


def test_move_to_wait_real_time():
    stage = SimLinearStage(max_velocity_um_s=20000, velocity_um_s=20000)
    stage.connect()
    t0 = time.monotonic()
    stage.move_to(1000, wait=True, timeout_s=2)
    assert time.monotonic() - t0 < 1.0
    assert stage.position_um == 1000
    assert stage.state is MotorState.IDLE


def test_wait_timeout():
    stage = SimLinearStage(max_velocity_um_s=10, velocity_um_s=10)
    stage.connect()
    with pytest.raises(TimeoutError):
        stage.move_to(1000, wait=True, timeout_s=0.05)


def test_move_outside_travel_rejected(z):
    with pytest.raises(ValueError):
        z.move_to(30000)
    with pytest.raises(ValueError):
        z.move_to(-1)


def test_immediate_stop_mid_move(z, clock):
    z.move_to(2000)
    clock.advance(0.5)
    z.stop()
    assert z.state is MotorState.IDLE
    assert z.position_um == pytest.approx(500)
    clock.advance(1.0)
    assert z.position_um == pytest.approx(500)


def test_profiled_stop_decelerates(z, clock):
    z.move_to(2000)
    clock.advance(0.5)
    z.stop(immediate=False)
    assert z.state is MotorState.STOPPING
    clock.advance(0.1)
    assert z.state is MotorState.STOPPING
    clock.advance(0.2)
    assert z.state is MotorState.IDLE
    # 500 µm + v*T/2 = 500 + 1000*0.2/2
    assert z.position_um == pytest.approx(600)


def test_jog_up_down_and_stop(z, clock):
    z.move_to(1000)
    clock.advance(2)
    z.jog(Direction.UP, 400)
    assert z.state is MotorState.MOVING
    clock.advance(1)
    assert z.position_um == pytest.approx(1400)
    z.stop()
    assert z.state is MotorState.IDLE
    z.jog(Direction.DOWN)  # uses set velocity (1000)
    clock.advance(0.5)
    z.stop()
    assert z.position_um == pytest.approx(900)


def test_jog_clamped_to_max_velocity(z, clock):
    z.jog(Direction.UP, 1e9)
    clock.advance(1)
    assert z.position_um == pytest.approx(2000)


def test_jog_stops_at_travel_limit(clock):
    stage = SimLinearStage(max_um=1000, velocity_um_s=1000, clock=clock)
    stage.connect()
    stage.jog(Direction.UP)
    clock.advance(5)
    assert stage.position_um == 1000
    assert stage.state is MotorState.IDLE
    # Jogging away from a limit works.
    stage.jog(Direction.DOWN)
    clock.advance(0.1)
    assert stage.position_um == pytest.approx(900)


def test_set_velocity_clamped(z):
    z.set_velocity(1e6)
    assert z.velocity_um_s == z.max_velocity_um_s
    with pytest.raises(ValueError):
        z.set_velocity(0)


def test_home(z, clock):
    z.move_to(300)
    clock.advance(1)
    assert not z.is_homed
    z.home()
    assert z.state is MotorState.HOMING
    clock.advance(1)
    assert z.state is MotorState.IDLE
    assert z.position_um == 0
    assert z.is_homed


def test_fault_reports_error_and_raises(z):
    z.fault = RuntimeError("motor fault")
    assert z.state is MotorState.ERROR
    with pytest.raises(RuntimeError):
        z.move_to(10)
    z.fault = None
    assert z.state is MotorState.IDLE


def test_rotator_move_and_jog(clock):
    rot = SimRotaryStage(velocity_deg_s=10, clock=clock)
    rot.connect()
    rot.move_to(45)
    clock.advance(2)
    assert rot.angle_deg == pytest.approx(20)
    assert rot.state is MotorState.MOVING
    clock.advance(3)
    assert rot.angle_deg == 45
    assert rot.state is MotorState.IDLE
    rot.jog(Direction.DOWN)
    clock.advance(10)
    rot.stop()
    assert rot.angle_deg == pytest.approx(-55)
    rot.home()
    clock.advance(10)
    assert rot.angle_deg == 0 and rot.is_homed


def test_concurrent_access_is_safe():
    stage = SimLinearStage(max_velocity_um_s=2600, velocity_um_s=2600)
    stage.connect()
    stage.move_to(12500)
    errors = []

    def hammer(n):
        try:
            for i in range(300):
                if n == 0:
                    stage.jog(Direction.UP if i % 2 else Direction.DOWN)
                    stage.stop()
                else:
                    _ = stage.position_um, stage.state, stage.velocity_um_s
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert stage.state is MotorState.IDLE
