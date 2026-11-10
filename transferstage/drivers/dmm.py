"""RTD thermometer read through a digital multimeter over VISA.

The LabVIEW VI ``Libs/DMM/GETtemperature.vi`` opens the VISA alias ``DMM`` and
queries ``MEAS:TEMP? RTD`` (SCPI); the reply is one number in °C. This driver
sends ``CONF:TEMP RTD`` once and then polls ``READ?`` (see ``QUERY``). The alias is
defined in NI MAX (or the VISA backend's alias table) and points at the meter.

Manual test CLI::

    python -m transferstage.drivers.dmm --alias DMM            # one reading
    python -m transferstage.drivers.dmm --alias DMM --watch    # once per second, Ctrl+C to stop
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from typing import Any

from .base import DeviceError, NotConnectedError, Thermometer

log = logging.getLogger(__name__)

# The LabVIEW VI polls ``MEAS:TEMP? RTD``. MEAS? = CONF + READ?, so every poll
# reconfigures the meter and its display blanks ("______") between readings.
# Configure once instead, then poll with READ? (trigger + fetch only).
CONFIGURE = "CONF:TEMP RTD"
QUERY = "READ?"

# SCPI meters report overload / open sensor as +/-9.9E37.
OVERLOAD = 9.0e37
# Plausible RTD range; anything outside means a wiring or configuration fault.
T_MIN_C, T_MAX_C = -250.0, 1000.0


def parse_temperature(reply: str) -> float:
    """Parse a ``READ?`` / ``MEAS:TEMP?`` reply such as ``"+2.345600E+01\\n"`` into °C."""
    text = reply.strip()
    if "," in text:  # some meters append units or a channel: take the first field
        text = text.split(",", 1)[0].strip()
    try:
        value = float(text)
    except ValueError as exc:
        raise DeviceError(f"DMM: cannot parse temperature reply {reply!r}") from exc
    if not math.isfinite(value) or abs(value) >= OVERLOAD:
        raise DeviceError(f"DMM: overload / open sensor (reply {reply!r})")
    if not T_MIN_C <= value <= T_MAX_C:
        raise DeviceError(f"DMM: temperature {value} °C outside plausible RTD range")
    return value


# VI_GPIB_REN_ADDRESS_GTL: send Go To Local to this device (pyvisa RENLineOperation.address_gtl).
REN_ADDRESS_GTL = 6
LOCAL_COMMAND = "SYST:LOC"


def go_to_local(res: Any) -> None:
    """Return the meter's front panel to local control. Never raises.

    GPIB and USB-TMC take the bus-level Go To Local (``control_ren``). Interfaces
    without REN (serial, LAN socket) raise there, so fall back to SCPI ``SYST:LOC``.
    """
    try:
        res.control_ren(REN_ADDRESS_GTL)
        log.debug("DMM: go to local (GTL)")
        return
    except Exception as exc:  # noqa: BLE001 - VisaIOError, NotImplementedError, AttributeError on fakes
        log.debug("DMM: GTL not available (%s), trying %s", exc, LOCAL_COMMAND)
    try:
        res.write(LOCAL_COMMAND)
        log.debug("DMM: go to local (%s)", LOCAL_COMMAND)
    except Exception as exc:  # noqa: BLE001
        log.warning("DMM: could not return meter to local: %s", exc)


class DmmThermometer(Thermometer):
    """Reads °C from the VISA resource ``visa_alias``.

    ``resource_manager`` is a ``pyvisa.ResourceManager`` (or a fake with
    ``open_resource``); by default one is created on :meth:`connect`.
    """

    def __init__(
        self,
        visa_alias: str,
        *,
        timeout_ms: int = 3000,
        resource_manager: Any = None,
    ) -> None:
        super().__init__()
        self.visa_alias = visa_alias
        self.timeout_ms = timeout_ms
        self._rm = resource_manager
        self._own_rm = resource_manager is None
        self._res: Any = None
        self._configured = False

    def connect(self) -> None:
        with self._lock:
            if self._res is not None:
                return
            try:
                if self._rm is None:
                    import pyvisa

                    self._rm = pyvisa.ResourceManager()
                res = self._rm.open_resource(self.visa_alias)
                res.timeout = self.timeout_ms
                res.read_termination = "\n"
                res.write_termination = "\n"
            except DeviceError:
                raise
            except Exception as exc:  # noqa: BLE001 - pyvisa.VisaIOError, ValueError for an unknown alias, ...
                raise DeviceError(f"DMM: cannot open VISA resource {self.visa_alias!r}: {exc}") from exc
            self._res = res

    def close(self) -> None:
        with self._lock:
            res, self._res = self._res, None
            self._configured = False
            if res is not None:
                go_to_local(res)
                try:
                    res.close()
                except Exception as exc:  # noqa: BLE001
                    log.warning("DMM close: %s", exc)
            if self._own_rm and self._rm is not None:
                try:
                    self._rm.close()
                except Exception as exc:  # noqa: BLE001
                    log.warning("DMM resource manager close: %s", exc)
                self._rm = None

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._res is not None

    def query_raw(self) -> str:
        with self._lock:
            if self._res is None:
                raise NotConnectedError(f"DMM {self.visa_alias!r} is not connected")
            try:
                if not self._configured:
                    self._res.write(CONFIGURE)
                    self._configured = True
                return self._res.query(QUERY)
            except Exception as exc:  # noqa: BLE001 - pyvisa.VisaIOError (timeout), OSError, ...
                # The meter may have been reset or power-cycled: configure again next time.
                self._configured = False
                raise DeviceError(f"DMM: {QUERY} failed: {exc}") from exc

    def read_temperature(self) -> float:
        return parse_temperature(self.query_raw())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m transferstage.drivers.dmm", description="Read the RTD temperature.")
    p.add_argument("--alias", default="DMM", help="VISA alias or resource name (default DMM)")
    p.add_argument("--watch", action="store_true", help="read repeatedly until Ctrl+C")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between readings with --watch")
    p.add_argument("--list", action="store_true", help="list VISA resources and exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    if args.list:
        import pyvisa

        rm = pyvisa.ResourceManager()
        for name, info in rm.list_resources_info().items():
            print(name, "alias:", info.alias)
        rm.close()
        return 0

    dmm = DmmThermometer(args.alias)
    try:
        dmm.connect()
        while True:
            t0 = time.monotonic()
            try:
                raw = dmm.query_raw()
                print(f"{time.strftime('%H:%M:%S')}  {parse_temperature(raw):8.3f} °C   (raw {raw.strip()!r})", flush=True)
            except DeviceError as exc:
                print(f"{time.strftime('%H:%M:%S')}  error: {exc}", file=sys.stderr, flush=True)
                if not args.watch:
                    return 1
            if not args.watch:
                return 0
            time.sleep(max(0.0, args.interval - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        return 0
    except DeviceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        dmm.close()


if __name__ == "__main__":
    sys.exit(main())
