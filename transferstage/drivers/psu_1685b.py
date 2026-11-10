"""B&K Precision 1685B power supply (1–60 V, 0–5 A) over its USB virtual COM port.

Protocol reference
------------------
*B&K Precision 1685B/1687B/1688B Programming Manual*, section 3 "Command Set"
(manual index: https://www.bkprecision.com/support/downloads/manuals/en, "Model
168xB"). The manual PDF could not be downloaded when this driver was written, so the
details below were cross-checked against two secondary sources that agree:

* B&K's own example library ``Python 3/gui/lib1685b.py`` in
  https://github.com/BKPrecisionCorp/1685B_Series (``voltMult = currMult = 10``,
  9600 baud, ``\\r`` terminator), and
* the command-set summary quoted from the manual in
  https://github.com/abduznik/instrumation/issues/270.

Summary (all commands end with ``\\r``; the supply answers ``OK\\r`` after every
command, and a query sends its data line first, then ``OK\\r``):

==============  =============================================================
``VOLTvvv``     voltage set point, 3 digits, volts x10   (``VOLT050`` = 5.0 V)
``CURRiii``     current limit, 3 digits, amps x100      (``CURR150`` = 1.50 A)
``SOUT0``       output **ON**  (inverted polarity: 0 = on)
``SOUT1``       output **OFF**
``GETS``        set points -> ``vvviii`` (V x10, A x100)
``GETD``        display readings -> ``vvvviiiis`` (V and A x100, s: 0 = CV, 1 = CC)
``GMAX``        maximum ratings -> ``vvviii`` (V x10, A x100)
==============  =============================================================

Serial settings: 9600 baud, 8 data bits, no parity, 1 stop bit, no flow control.

Current is amps x100 on this 1685B, as PROMPTS.md (from the LabVIEW program) said;
the secondary sources above say x10, which is wrong for this unit. Confirmed on the
instrument on 2026-11-09: with ``CURR030`` (read as 3.0 A under x10) the supply went
constant-current at 0.31 A, and ``GMAX`` answers ``605680`` (60.5 V / 6.80 A; x10
would mean 68 A). The ``--read`` CLI prints the raw replies.

The supply has no output-state query, so :attr:`Psu1685B.is_on` is the last
commanded state. By default :meth:`Psu1685B.connect` turns the output off so that
the commanded and actual states agree, and :meth:`Psu1685B.close` turns it off
again before releasing the port.

Manual test CLI (see ``--help``)::

    python -m transferstage.drivers.psu_1685b --port COM4 --read
    python -m transferstage.drivers.psu_1685b --port COM4 --volt 5 --curr 0.5 --on --read
    python -m transferstage.drivers.psu_1685b --port COM4 --off
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from typing import Any, Callable

from .base import DeviceError, NotConnectedError, PowerSupply, PsuMode, PsuReading

log = logging.getLogger(__name__)

TERMINATOR = b"\r"
ACK = "OK"
BAUD = 9600

V_SCALE = 10  # VOLT / GETS / GMAX voltage digits per volt
I_SCALE = 100  # CURR / GETS / GMAX current digits per amp
DISPLAY_SCALE = 100  # GETD digits per volt / amp

# 1685B ratings, used until GMAX is read on connect.
DEFAULT_V_MAX = 60.0
DEFAULT_I_MAX = 5.0
# Lowest programmable voltage. The supply never answers a VOLT below 1.0 V (VOLT000 times out,
# confirmed on the instrument), so lower set points are sent as 1.0 V; use the output switch for 0 V.
V_MIN = 1.0


# --------------------------------------------------------------------------- protocol helpers (pure)


def _setpoint_digits(value: float, scale: int, maximum: float, what: str) -> int:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{what} must be finite, got {value!r}")
    if value < 0 or value > maximum + 1e-9:
        raise ValueError(f"{what} {value} outside the supply range 0–{maximum}")
    digits = round(value * scale)
    if digits > 999:
        raise ValueError(f"{what} {value} does not fit in 3 digits")
    return digits


def format_voltage(v: float, v_max: float = DEFAULT_V_MAX) -> str:
    """``5.0`` -> ``"VOLT050"`` (rounded to the 0.1 V resolution; 0 <= v < 1 V -> ``"VOLT010"``)."""
    digits = _setpoint_digits(v, V_SCALE, v_max, "voltage")
    return f"VOLT{max(digits, round(V_MIN * V_SCALE)):03d}"


def format_current(a: float, i_max: float = DEFAULT_I_MAX) -> str:
    """``1.5`` -> ``"CURR150"`` (rounded to the 0.01 A resolution)."""
    return f"CURR{_setpoint_digits(a, I_SCALE, i_max, 'current'):03d}"


def format_output(on: bool) -> str:
    """Output control. The polarity is inverted: ``SOUT0`` is ON."""
    return "SOUT0" if on else "SOUT1"


def _digits(reply: str, n: int, what: str) -> str:
    s = reply.strip()
    if len(s) != n or not s.isdigit():
        raise DeviceError(f"unexpected {what} reply {reply!r} (expected {n} digits)")
    return s


def parse_getd(reply: str) -> tuple[float, float, PsuMode]:
    """``"050001231"`` -> ``(5.0, 1.23, CC)``: ``vvvv iiii s`` with V and A x100."""
    s = _digits(reply, 9, "GETD")
    v = int(s[0:4]) / DISPLAY_SCALE
    i = int(s[4:8]) / DISPLAY_SCALE
    if s[8] == "0":
        mode = PsuMode.CV
    elif s[8] == "1":
        mode = PsuMode.CC
    else:
        raise DeviceError(f"unexpected GETD status digit in {reply!r}")
    return v, i, mode


def parse_gets(reply: str) -> tuple[float, float]:
    """``"050150"`` -> ``(5.0, 1.5)``: set points ``vvv iii``, V x10 and A x100."""
    s = _digits(reply, 6, "GETS")
    return int(s[0:3]) / V_SCALE, int(s[3:6]) / I_SCALE


def parse_gmax(reply: str) -> tuple[float, float]:
    """``"605680"`` -> ``(60.5, 6.8)``: maximum ratings ``vvv iii``, V x10 and A x100."""
    s = _digits(reply, 6, "GMAX")
    return int(s[0:3]) / V_SCALE, int(s[3:6]) / I_SCALE


# --------------------------------------------------------------------------- driver


def _open_serial(port: str, timeout_s: float) -> Any:
    import serial  # pyserial; imported lazily so tests don't need it

    return serial.Serial(
        port=port,
        baudrate=BAUD,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        xonxoff=False,
        rtscts=False,
        timeout=timeout_s,
        write_timeout=timeout_s,
    )


class Psu1685B(PowerSupply):
    """B&K 1685B on a serial port.

    Set points below :data:`V_MIN` are sent as ``V_MIN`` (see :func:`format_voltage`).

    ``serial_factory(port, timeout_s)`` returns an open pyserial-like object
    (``write``, ``read_until``, ``reset_input_buffer``, ``close``); tests pass a fake.
    ``off_on_connect`` / ``off_on_close`` send ``SOUT1`` at those points (default on:
    the heater must never be left energised by accident).
    """

    v_min = V_MIN

    def __init__(
        self,
        port: str,
        *,
        timeout_s: float = 1.0,
        serial_factory: Callable[[str, float], Any] | None = None,
        off_on_connect: bool = True,
        off_on_close: bool = True,
    ) -> None:
        super().__init__()
        self.port = port
        self.timeout_s = timeout_s
        self._factory = serial_factory or _open_serial
        self.off_on_connect = off_on_connect
        self.off_on_close = off_on_close
        self._ser: Any = None
        self._on = False
        self.v_max = DEFAULT_V_MAX
        self.i_max = DEFAULT_I_MAX

    # ------------------------------------------------------------------ transport

    def _readline(self) -> str:
        raw = self._ser.read_until(TERMINATOR)
        if not raw or not raw.endswith(TERMINATOR):
            raise DeviceError(f"1685B on {self.port}: no reply within {self.timeout_s} s (got {raw!r})")
        return raw[:-1].decode("ascii", errors="replace").strip()

    def _transact(self, cmd: str, *, query: bool) -> str | None:
        """Send ``cmd``; return the data line for a query. Always consumes the ``OK``."""
        if self._ser is None:
            raise NotConnectedError(f"1685B on {self.port} is not connected")
        self._ser.reset_input_buffer()
        self._ser.write(cmd.encode("ascii") + TERMINATOR)
        data = None
        if query:
            data = self._readline()
            if data == ACK:
                raise DeviceError(f"1685B: {cmd} returned OK without data")
        ack = self._readline()
        if ack != ACK:
            raise DeviceError(f"1685B: {cmd} answered {ack!r}, expected {ACK!r}")
        log.debug("1685B %s -> %r", cmd, data)
        return data

    def command(self, cmd: str) -> None:
        """Send a raw command and check the ``OK`` acknowledgement."""
        with self._lock:
            self._transact(cmd, query=False)

    def query(self, cmd: str) -> str:
        """Send a raw query; return its data line."""
        with self._lock:
            data = self._transact(cmd, query=True)
            assert data is not None
            return data

    # ------------------------------------------------------------------ Device

    def connect(self) -> None:
        with self._lock:
            if self._ser is not None:
                return
            try:
                self._ser = self._factory(self.port, self.timeout_s)
            except Exception as exc:  # noqa: BLE001 - serial.SerialException, OSError, ...
                raise DeviceError(f"cannot open {self.port}: {exc}") from exc
            try:
                try:
                    self.v_max, self.i_max = parse_gmax(self.query("GMAX"))
                except DeviceError as exc:
                    # A unit that ignores GMAX still works; anything with no reply at all fails below.
                    log.warning("1685B GMAX not understood (%s); assuming %s V / %s A", exc, DEFAULT_V_MAX, DEFAULT_I_MAX)
                    self.v_max, self.i_max = DEFAULT_V_MAX, DEFAULT_I_MAX
                    self.query("GETS")
                if self.off_on_connect:
                    self._transact(format_output(False), query=False)
                    self._on = False
            except Exception:
                self._close_port()
                raise

    def _close_port(self) -> None:
        ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("closing %s: %s", self.port, exc)

    def close(self) -> None:
        with self._lock:
            if self._ser is None:
                return
            if self.off_on_close:
                try:
                    self._transact(format_output(False), query=False)
                    self._on = False
                except Exception as exc:  # noqa: BLE001 - close must not raise
                    log.error("1685B: could not turn output off on close: %s", exc)
            self._close_port()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._ser is not None

    # ------------------------------------------------------------------ PowerSupply

    def set_voltage(self, v: float) -> None:
        with self._lock:
            self._transact(format_voltage(v, self.v_max), query=False)

    def set_current(self, a: float) -> None:
        with self._lock:
            self._transact(format_current(a, self.i_max), query=False)

    def output(self, on: bool) -> None:
        with self._lock:
            self._transact(format_output(bool(on)), query=False)
            self._on = bool(on)

    def read_output(self) -> PsuReading:
        with self._lock:
            v, i, mode = parse_getd(self.query("GETD"))
            return PsuReading(voltage=v, current=i, mode=mode if self._on else PsuMode.OFF)

    def read_setpoints(self) -> tuple[float, float]:
        """Return the programmed (voltage, current limit)."""
        with self._lock:
            return parse_gets(self.query("GETS"))

    @property
    def is_on(self) -> bool:
        with self._lock:
            return self._on


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m transferstage.drivers.psu_1685b",
        description="Manual test of the B&K 1685B. Actions run in the order: --curr, --volt, --on/--off, --read.",
    )
    p.add_argument("--port", default="COM4")
    p.add_argument("--volt", type=float, help="voltage set point, V")
    p.add_argument("--curr", type=float, help="current limit, A")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--on", action="store_true", help="enable the output (SOUT0)")
    g.add_argument("--off", action="store_true", help="disable the output (SOUT1)")
    p.add_argument("--read", action="store_true", help="print GMAX, GETS and GETD (raw and parsed)")
    p.add_argument("--max-volt", type=float, default=24.0, help="refuse --volt above this (default 24 V)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    if args.volt is not None and args.volt > args.max_volt:
        p.error(f"--volt {args.volt} exceeds --max-volt {args.max_volt}")
    if not any([args.volt is not None, args.curr is not None, args.on, args.off, args.read]):
        args.read = True

    # --on/--off/--volt leave the output as commanded; only an explicit --off turns it off.
    psu = Psu1685B(args.port, off_on_connect=False, off_on_close=False)
    try:
        psu.connect()
        print(f"connected to {args.port}: GMAX {psu.v_max} V / {psu.i_max} A")
        if args.curr is not None:
            psu.set_current(args.curr)
            print(f"sent {format_current(args.curr, psu.i_max)}: OK")
        if args.volt is not None:
            psu.set_voltage(args.volt)
            print(f"sent {format_voltage(args.volt, psu.v_max)}: OK")
        if args.on or args.off:
            psu.output(args.on)
            print(f"sent {format_output(args.on)} (output {'ON' if args.on else 'OFF'}): OK")
            time.sleep(0.5)
        if args.read:
            for cmd, parse in (("GMAX", parse_gmax), ("GETS", parse_gets), ("GETD", parse_getd)):
                raw = psu.query(cmd)
                try:
                    parsed = parse(raw)
                except DeviceError as exc:
                    parsed = f"PARSE ERROR: {exc}"
                print(f"{cmd}: raw {raw!r} -> {parsed}")
    except (DeviceError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        psu.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
