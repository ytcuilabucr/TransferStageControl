import pytest

from transferstage.config import Config, config_from_dict
from transferstage.drivers import DeviceError, NotConnectedError, build_devices
from transferstage.drivers.dmm import CONFIGURE, LOCAL_COMMAND, QUERY, REN_ADDRESS_GTL, DmmThermometer, parse_temperature


class FakeResource:
    def __init__(self, replies):
        self.replies = list(replies)
        self.queries = []
        self.closed = False
        self.timeout = None
        self.ren = []
        self.writes = []
        self.ren_error = None

    def control_ren(self, mode):
        if self.ren_error is not None:
            raise self.ren_error
        self.ren.append(mode)

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        self.queries.append(cmd)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        self.closed = True


class FakeRM:
    def __init__(self, resource):
        self.resource = resource
        self.opened = []

    def open_resource(self, name):
        self.opened.append(name)
        if isinstance(self.resource, Exception):
            raise self.resource
        return self.resource


@pytest.mark.parametrize(
    "reply, t", [("+2.345600E+01\n", 23.456), ("25.0", 25.0), ("-1.5e1", -15.0), ("+1.0E+02,C", 100.0)]
)
def test_parse_temperature(reply, t):
    assert parse_temperature(reply) == pytest.approx(t)


@pytest.mark.parametrize("reply", ["", "abc", "+9.90000000E+37", "-9.9E37", "nan", "5000"])
def test_parse_temperature_rejects(reply):
    with pytest.raises(DeviceError):
        parse_temperature(reply)


def test_read_temperature():
    res = FakeResource(["+2.5E+01\n"])
    rm = FakeRM(res)
    with DmmThermometer("DMM", resource_manager=rm, timeout_ms=1234) as dmm:
        assert dmm.read_temperature() == 25.0
    assert rm.opened == ["DMM"] and res.queries == [QUERY]
    assert res.writes == [CONFIGURE]  # GTL went over control_ren, no SYST:LOC
    assert res.timeout == 1234 and res.closed


def test_configures_once_then_reads():
    res = FakeResource(["+2.5E+01", "+2.6E+01", TimeoutError("tmo"), "+2.7E+01"])
    dmm = DmmThermometer("DMM", resource_manager=FakeRM(res))
    dmm.connect()
    assert dmm.read_temperature() == 25.0
    assert dmm.read_temperature() == 26.0
    assert res.writes == [CONFIGURE] and res.queries == [QUERY, QUERY]
    with pytest.raises(DeviceError):
        dmm.read_temperature()
    assert dmm.read_temperature() == 27.0  # reconfigures after an error
    assert res.writes == [CONFIGURE, CONFIGURE]


def test_close_returns_meter_to_local():
    res = FakeResource([])
    dmm = DmmThermometer("DMM", resource_manager=FakeRM(res))
    dmm.connect()
    dmm.close()
    assert res.ren == [REN_ADDRESS_GTL] and res.writes == [] and res.closed
    dmm.close()  # idempotent
    assert res.ren == [REN_ADDRESS_GTL]


def test_close_falls_back_to_scpi_local():
    res = FakeResource([])
    res.ren_error = NotImplementedError("no REN on serial")
    dmm = DmmThermometer("DMM", resource_manager=FakeRM(res))
    dmm.connect()
    dmm.close()
    assert res.writes == [LOCAL_COMMAND] and res.closed  # never read, so never configured


def test_timeout_becomes_device_error():
    dmm = DmmThermometer("DMM", resource_manager=FakeRM(FakeResource([TimeoutError("VI_ERROR_TMO")])))
    dmm.connect()
    with pytest.raises(DeviceError, match="VI_ERROR_TMO"):
        dmm.read_temperature()


def test_open_failure():
    dmm = DmmThermometer("NOPE", resource_manager=FakeRM(ValueError("unknown alias")))
    with pytest.raises(DeviceError, match="NOPE"):
        dmm.connect()
    assert not dmm.is_connected


def test_not_connected():
    with pytest.raises(NotConnectedError):
        DmmThermometer("DMM", resource_manager=FakeRM(None)).read_temperature()


def test_build_devices_uses_real_dmm():
    cfg = config_from_dict({"devices": {"dmm_visa": "TEMP", "simulate": {"dmm": False}}})
    devs = build_devices(cfg)
    assert isinstance(devs.thermometer, DmmThermometer) and devs.thermometer.visa_alias == "TEMP"
    assert devs.plant is None


@pytest.mark.hardware
def test_hardware_dmm_reads():
    with DmmThermometer(Config().devices.dmm_visa) as dmm:
        assert 0 < dmm.read_temperature() < 300
