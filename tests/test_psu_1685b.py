import pytest

from transferstage.config import Config, config_from_dict
from transferstage.drivers import DeviceError, NotConnectedError, PsuMode, build_devices
from transferstage.drivers.psu_1685b import (
    Psu1685B,
    format_current,
    format_output,
    format_voltage,
    parse_getd,
    parse_gets,
    parse_gmax,
)


class FakeSerial:
    """Scripted 1685B: replies to each command per ``responses`` (default: just OK)."""

    def __init__(self, responses=None):
        self.responses = {"GMAX": ["605680", "OK"], **(responses or {})}
        self.written: list[str] = []
        self._buf = b""
        self.closed = False

    def reset_input_buffer(self):
        self._buf = b""

    def write(self, data: bytes):
        assert data.endswith(b"\r")
        cmd = data[:-1].decode()
        self.written.append(cmd)
        lines = self.responses.get(cmd, self.responses.get(cmd[:4], ["OK"]))
        self._buf += b"".join(line.encode() + b"\r" for line in lines)

    def read_until(self, terminator=b"\r"):
        i = self._buf.find(terminator)
        if i < 0:  # timeout: return what is there
            out, self._buf = self._buf, b""
            return out
        out, self._buf = self._buf[: i + 1], self._buf[i + 1 :]
        return out

    def close(self):
        self.closed = True


def make(responses=None, **kw):
    fake = FakeSerial(responses)
    psu = Psu1685B("COMX", serial_factory=lambda port, t: fake, **kw)
    return psu, fake


# ------------------------------------------------------------------ formatting


@pytest.mark.parametrize(
    "v, cmd", [(0, "VOLT010"), (0.5, "VOLT010"), (1.0, "VOLT010"), (5, "VOLT050"), (12.34, "VOLT123"), (12.36, "VOLT124"), (60, "VOLT600")]
)
def test_format_voltage(v, cmd):
    assert format_voltage(v) == cmd


@pytest.mark.parametrize("a, cmd", [(0, "CURR000"), (0.5, "CURR050"), (1.5, "CURR150"), (3, "CURR300"), (1.234, "CURR123"), (5, "CURR500")])
def test_format_current(a, cmd):
    assert format_current(a) == cmd


@pytest.mark.parametrize("bad", [-0.1, 60.1, float("nan"), float("inf")])
def test_format_voltage_rejects_out_of_range(bad):
    with pytest.raises(ValueError):
        format_voltage(bad)


def test_format_current_respects_gmax():
    with pytest.raises(ValueError):
        format_current(5.1)
    assert format_current(6.8, i_max=6.8) == "CURR680"
    with pytest.raises(ValueError):
        format_current(10, i_max=10)  # 1000 does not fit in 3 digits


def test_output_polarity_is_inverted():
    assert format_output(True) == "SOUT0"
    assert format_output(False) == "SOUT1"


# ------------------------------------------------------------------ parsing


def test_parse_getd():
    assert parse_getd("050001231") == (5.0, 1.23, PsuMode.CC)
    assert parse_getd("240000000\r") == (24.0, 0.0, PsuMode.CV)


@pytest.mark.parametrize("bad", ["", "OK", "05000123", "0500012345", "05000123x", "050001232"])
def test_parse_getd_rejects_garbage(bad):
    with pytest.raises(DeviceError):
        parse_getd(bad)


def test_parse_gets_and_gmax():
    assert parse_gets("050150") == (5.0, 1.5)
    assert parse_gets("010030") == (1.0, 0.3)  # what the supply reported with the old x10 bug
    assert parse_gmax("605680") == (60.5, 6.8)  # the real 1685B
    with pytest.raises(DeviceError):
        parse_gets("50015")


# ------------------------------------------------------------------ driver


def test_connect_reads_gmax_and_forces_output_off():
    psu, fake = make({"GMAX": ["605680", "OK"]})
    psu.connect()
    assert fake.written == ["GMAX", "SOUT1"]
    assert (psu.v_max, psu.i_max) == (60.5, 6.8)
    assert psu.is_connected and not psu.is_on


def test_commands_and_readback():
    psu, fake = make({"GETD": ["050001230", "OK"], "GETS": ["050150", "OK"]})
    psu.connect()
    psu.set_current(1.5)
    psu.set_voltage(5)
    psu.output(True)
    assert fake.written[-3:] == ["CURR150", "VOLT050", "SOUT0"]
    assert psu.is_on
    assert psu.read_setpoints() == (5.0, 1.5)
    r = psu.read_output()
    assert (r.voltage, r.current, r.mode) == (5.0, 1.23, PsuMode.CV)
    psu.output(False)
    assert psu.read_output().mode is PsuMode.OFF


def test_close_turns_output_off_and_closes_port():
    psu, fake = make()
    psu.connect()
    psu.output(True)
    psu.close()
    assert fake.written[-1] == "SOUT1"
    assert fake.closed and not psu.is_connected
    psu.close()  # idempotent


def test_cli_style_no_auto_off():
    psu, fake = make(off_on_connect=False, off_on_close=False)
    psu.connect()
    psu.close()
    assert fake.written == ["GMAX"]


def test_bad_ack_raises():
    psu, _ = make({"VOLT": ["ERR"]})
    psu.connect()
    with pytest.raises(DeviceError, match="expected 'OK'"):
        psu.set_voltage(5)


def test_no_reply_raises():
    psu, _ = make({"VOLT": []})
    psu.connect()
    with pytest.raises(DeviceError, match="no reply"):
        psu.set_voltage(5)


def test_gmax_unsupported_falls_back_to_defaults():
    psu, fake = make({"GMAX": ["OK"], "GETS": ["000050", "OK"]})
    psu.connect()
    assert (psu.v_max, psu.i_max) == (60.0, 5.0)
    assert fake.written == ["GMAX", "GETS", "SOUT1"]


def test_connect_fails_cleanly_when_silent():
    psu, fake = make({"GMAX": [], "GETS": []})
    with pytest.raises(DeviceError):
        psu.connect()
    assert fake.closed and not psu.is_connected


def test_not_connected():
    psu, _ = make()
    with pytest.raises(NotConnectedError):
        psu.set_voltage(1)


def test_out_of_range_not_sent():
    psu, fake = make()
    psu.connect()
    n = len(fake.written)
    with pytest.raises(ValueError):
        psu.set_voltage(61)
    assert len(fake.written) == n


def test_build_devices_uses_real_psu():
    cfg = config_from_dict({"devices": {"psu_port": "COM9", "simulate": {"psu": False}}})
    devs = build_devices(cfg)
    assert isinstance(devs.psu, Psu1685B) and devs.psu.port == "COM9"
    assert not devs.psu.is_connected


@pytest.mark.hardware
def test_hardware_psu_roundtrip():
    with Psu1685B(Config().devices.psu_port) as psu:
        psu.set_current(0.5)
        psu.set_voltage(2.0)
        assert psu.read_setpoints() == (2.0, 0.5)
        assert not psu.is_on
