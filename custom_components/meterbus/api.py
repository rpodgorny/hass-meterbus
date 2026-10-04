"""Meter-Bus (EN 13757-2/3) master client.

Pure Python, blocking I/O, no Home Assistant imports — so it is testable offline
and runnable standalone as a bus dump tool::

    python3 api.py /dev/ttyUSB0
    python3 api.py 192.168.11.9:10001
    python3 api.py --scan /dev/ttyUSB0

Wire framing, DIF/VIF decoding and secondary addressing come from `pyMeterBus
<https://github.com/ganehag/pyMeterBus>`_; this module is the master state
machine on top of it — select, request, follow multi-telegram replies, and the
wildcard tree search that finds meters without anyone typing an address.

ponytail: no transport class of our own. pyserial's ``serial_for_url`` already
speaks ``socket://host:port``, so a USB M-Bus master and a ser2net bridge are
the same code path with a different URL.

Meters are addressed by their **secondary** address (16 hex digits) rather than
a primary one. Primary addresses are settable, frequently left at 0 on every
meter in a building, and say nothing about which meter is which; the secondary
address is the meter's identity — manufacturer, serial, version and medium —
and it is what is printed on the device.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import meterbus

_LOGGER = logging.getLogger(__name__)

DEFAULT_BAUD = 2400
DEFAULT_TCP_PORT = 10001
DEFAULT_SERIAL_PORT = "/dev/ttyUSB0"

# M-Bus is 8E1. Even parity is not a preference: a meter will not answer at all
# with the wrong parity, which looks exactly like a dead bus.
_BYTESIZE = 8
_PARITY = "E"
_STOPBITS = 1

# A 2400-baud long frame is ~0.4 s on the wire, and meters take their time
# answering. Two seconds covers both without stalling the whole sweep on a
# meter that is simply not there.
DEFAULT_TIMEOUT = 2.0

# A select is answered with a single-byte ACK, so it needs far less patience
# than a data frame — and a scan spends most of its life waiting out selects
# that nothing answers. At the full timeout a scan of a real bus takes tens of
# minutes; at this one it is a few.
SELECT_TIMEOUT = 0.5

# After a collision several meters are still talking over each other. Sending
# the next select straight away reads their tail as another collision, which
# recurses, which collides again — the scan never converges. Wait for the bus
# to go quiet, then drop whatever arrived.
_COLLISION_SETTLE = 0.3

# Outcomes of one address-mask probe.
_EMPTY = "empty"
_SINGLE = "single"
_COLLISION = "collision"

# When a prefix that just collided yields no meters at all, the walk is redone
# with timeouts stretched by this much. A slow meter answering late is one of
# the few things that produces that contradiction.
_RETRY_TIMEOUT_FACTOR = 3.0

WILDCARD = "FFFFFFFFFFFFFFFF"

# A meter answering "more records follow" forever would hang the whole sweep.
# Sixteen telegrams is far past any real meter (the most seen here is two) and
# still bounded.
_MAX_TELEGRAMS = 16

# Medium codes from the fixed header (EN 13757-3). Only used for naming, so an
# unknown code degrades to its number rather than failing anything.
MEDIUMS = {
    0x00: "Other",
    0x01: "Oil",
    0x02: "Electricity",
    0x03: "Gas",
    0x04: "Heat",
    0x05: "Steam",
    0x06: "Hot water",
    0x07: "Water",
    0x08: "Heat cost allocator",
    0x09: "Compressed air",
    0x0A: "Cooling load",
    0x0B: "Cooling load",
    0x0C: "Heat inlet",
    0x0D: "Heat / cooling load",
    0x0E: "Bus / system",
    0x0F: "Unknown",
    0x15: "Hot water",
    0x16: "Cold water",
    0x17: "Dual water",
    0x18: "Pressure",
    0x19: "A/D converter",
}


class MeterBusError(Exception):
    """Transport, framing or protocol failure."""


@dataclass(frozen=True)
class Record:
    """One decoded data record of a meter's reply.

    ``vif`` and ``unit`` are pyMeterBus's own enum names as strings (for example
    ``"VIFUnit.VOLUME"`` and ``"MeasureUnit.M3"``). Keeping them as strings is
    deliberate: it makes the profile table readable, diffable and testable
    without importing pyMeterBus, and it survives the library renaming an enum
    member without changing its wire meaning.

    ``value`` is already scaled by the VIF exponent — pyMeterBus returns m³ and
    Wh, not "milli-m³" and "100 Wh" as the wire carries them.
    """

    vif: str
    unit: str
    function: str
    storage: int
    tariff: int | None
    value: object
    device: int = 0


@dataclass(frozen=True)
class MeterReading:
    """One meter's complete reply."""

    address: str
    manufacturer: str
    medium: int
    version: int
    status: int
    records: tuple[Record, ...]

    @property
    def identification(self) -> str:
        """The meter's serial number, as printed on it (first half of the address)."""
        return self.address[:8]

    @property
    def medium_name(self) -> str:
        """Human-readable medium, or the raw code if it is not a known one."""
        return MEDIUMS.get(self.medium, f"Medium 0x{self.medium:02X}")


def _interpret(telegram) -> MeterReading:
    """Turn a parsed pyMeterBus telegram into a :class:`MeterReading`."""
    header = telegram.body.bodyHeader
    records = []
    for rec in telegram.records:
        try:
            data = rec.interpreted
        except Exception as err:  # noqa: BLE001 - any decode bug, same handling
            # One record the decoder chokes on must not cost the whole meter.
            # Skipping it loses that value; raising would lose all of them, and
            # the ones that follow are usually the interesting ones.
            _LOGGER.debug("skipping undecodable record: %s", err)
            continue
        records.append(
            Record(
                vif=str(data.get("type")),
                unit=str(data.get("unit")),
                function=str(data.get("function")),
                storage=int(data.get("storage_number", 0)),
                tariff=data.get("tariff"),
                value=data.get("value"),
                device=int(data.get("device") or 0),
            )
        )
    return MeterReading(
        address=telegram.secondary_address.upper(),
        manufacturer=header.manufacturer_field.decodeManufacturer,
        medium=header.measure_medium_field.parts[0],
        version=header.version_field.parts[0],
        status=header.status_field.parts[0],
        records=tuple(records),
    )


_SECONDARY_ADDRESS = re.compile(r"[0-9A-F]{16}")


def parse_addresses(text: str) -> tuple[list[str], list[str]]:
    """Split user-entered text into (valid addresses, rejected tokens).

    Accepts whatever separators someone actually pastes — newlines, commas,
    spaces, semicolons — because the list usually comes out of another tool's
    output rather than being typed.
    """
    tokens = [t for t in re.split(r"[\s,;]+", text.strip().upper()) if t]
    valid = [t for t in tokens if _SECONDARY_ADDRESS.fullmatch(t)]
    bad = [t for t in tokens if not _SECONDARY_ADDRESS.fullmatch(t)]
    return sorted(set(valid)), bad


def _matches(address: str, mask: str) -> bool:
    """True if ``address`` fits ``mask``, where ``F`` in the mask is a wildcard."""
    return len(address) == len(mask) and all(
        m == "F" or m == a for a, m in zip(address.upper(), mask.upper())
    )


def parse_frame(data: bytes) -> MeterReading:
    """Decode one long-frame reply. Exposed for tests against captured frames."""
    telegram = meterbus.load(list(data))
    if not isinstance(telegram, meterbus.TelegramLong):
        raise MeterBusError(f"not a data reply: {type(telegram).__name__}")
    return _interpret(telegram)


class MeterBus:
    """Blocking M-Bus master on one serial port or TCP bridge."""

    def __init__(
        self,
        *,
        serial_port: str | None = None,
        host: str | None = None,
        tcp_port: int = DEFAULT_TCP_PORT,
        baud: int = DEFAULT_BAUD,
        timeout: float = DEFAULT_TIMEOUT,
        port: Any = None,
    ) -> None:
        """Configure the master. ``port`` is an injection point for tests."""
        if not (serial_port or host or port):
            raise ValueError("need serial_port, host or port")
        self._url = serial_port or f"socket://{host}:{tcp_port}"
        self._baud = baud
        self._timeout_value = timeout
        self._port: Any = port
        # Only a port we opened ourselves can be reopened after a failure. An
        # injected one has no recipe to rebuild it from, so dropping it on a
        # retry would leave the client permanently dead.
        self._owns_port = port is None
        # How long to wait for an acknowledgement. Held on the instance so the
        # scan can stretch it when a branch looks like it answered too slowly.
        self._select_timeout = SELECT_TIMEOUT
        # One bus, one master, one transaction at a time. A polling sweep and a
        # discovery scan both want the wire, and M-Bus has no arbitration: two
        # conversations interleaved mid-transaction corrupt each other. Held
        # only for the length of a single transaction, so the two take turns
        # rather than one locking the other out for minutes.
        self._lock = threading.RLock()
        # M-Bus alternates the FCB bit on consecutive requests so a meter can
        # tell a retry from a request for the *next* telegram. Tracked per
        # master, reset by the SND_NKE each read starts with.
        self._fcb = False

    # -- connection ------------------------------------------------------

    def connect(self):
        """Open the port if it is not already open, and return it."""
        if self._port is None:
            import serial  # imported lazily so tests need no pyserial

            self._port = serial.serial_for_url(
                self._url,
                self._baud,
                _BYTESIZE,
                _PARITY,
                _STOPBITS,
                timeout=self._timeout_value,
            )
        return self._port

    def close(self) -> None:
        """Close the port, best effort."""
        if self._port is None:
            return
        try:
            self._port.close()
        except OSError:
            pass
        if self._owns_port:
            self._port = None

    # -- framing ---------------------------------------------------------

    def _flush(self) -> None:
        """Drop anything already buffered, so a stale tail is not read as a reply."""
        port = self.connect()
        try:
            port.reset_input_buffer()
        except (AttributeError, OSError):
            pass

    def _timeout(self, seconds: float) -> None:
        """Set how long the next read waits. Cheap on both serial and socket://."""
        port = self.connect()
        try:
            port.timeout = seconds
        except (AttributeError, OSError):
            pass

    def _read_reply(self) -> tuple[Any, bool]:
        """Read one reply. Returns ``(telegram or None, whether any byte came)``.

        The two halves are separate answers to separate questions. "Did it
        decode" says whether one meter is talking; "did anything arrive at all"
        says whether *anybody* is out there. During a wildcard scan those differ
        constantly — several meters answering at once produce bytes that decode
        to nothing — and conflating them is what loses meters.
        """
        raw = meterbus.recv_frame(self.connect())
        if raw is None:
            return None, False
        if raw is False:
            return None, True
        try:
            return meterbus.load(raw), True
        except (meterbus.MBusFrameDecodeError, meterbus.MBusFrameCRCError):
            return None, True

    def _recv(self):
        """Read one frame. Returns a parsed telegram, or raises."""
        telegram, saw_bytes = self._read_reply()
        if telegram is None:
            raise MeterBusError("garbled reply" if saw_bytes else "no reply")
        return telegram

    def _request(self, first: bool):
        """Send REQ_UD2 to the selected meter and return its reply telegram.

        ``first`` starts a fresh multi-telegram sequence; subsequent calls
        toggle FCB so the meter sends the *next* telegram rather than repeating
        the last one.
        """
        self._fcb = True if first else not self._fcb
        frame = meterbus.TelegramShort()
        control = meterbus.CONTROL_MASK_REQ_UD2 | meterbus.CONTROL_MASK_DIR_M2S
        if self._fcb:
            control |= meterbus.CONTROL_MASK_FCB
        frame.header.cField.parts = [control]
        frame.header.aField.parts = [meterbus.ADDRESS_NETWORK_LAYER]
        meterbus.serial_send(self.connect(), frame)
        return self._recv()

    def _init(self) -> None:
        """SND_NKE to the network layer: clear any half-finished conversation.

        Without it a meter left mid-multi-telegram by a previous master (or by
        a crashed poll) answers with the *next* telegram of that old sequence,
        so the first read after a restart returns the wrong half of a meter.
        """
        self._timeout(self._select_timeout)
        try:
            meterbus.send_ping_frame(self.connect(), meterbus.ADDRESS_NETWORK_LAYER)
            self._recv()
        except (MeterBusError, meterbus.MBusFrameDecodeError):
            # An unanswered SND_NKE is normal — it is a broadcast, and not every
            # meter acknowledges it. Waiting the full data timeout for a reply
            # that usually never comes would add a minute to every sweep.
            pass
        finally:
            self._timeout(self._timeout_value)

    def _send_select(self, address: str) -> None:
        """Send a select frame for one secondary address or wildcard mask."""
        meterbus.send_select_frame(self.connect(), address)

    def _select(self, address: str) -> bool:
        """Select one meter by secondary address, for a point-to-point read.

        Returns True on a clean ACK, False when the reply was unreadable.
        Raises when nothing answers at all. The scan does *not* use this — see
        :meth:`_probe` for why an ACK is not a trustworthy verdict there.
        """
        self._flush()
        self._timeout(self._select_timeout)
        try:
            self._send_select(address)
            frame = self._recv()
        except (meterbus.MBusFrameCRCError, meterbus.MBusFrameDecodeError):
            self._settle()
            return False
        except MeterBusError as err:
            if "garbled" in str(err):
                self._settle()
                return False
            raise
        finally:
            self._timeout(self._timeout_value)
        if isinstance(frame, meterbus.TelegramACK):
            return True
        raise MeterBusError(f"select answered {type(frame).__name__}, expected ACK")

    def _settle(self) -> None:
        """Let a colliding bus go quiet, then drop what it said."""
        time.sleep(_COLLISION_SETTLE)
        self._flush()

    # -- reading ---------------------------------------------------------

    def read(self, address: str) -> MeterReading:
        """Read one meter by its 16-hex-digit secondary address."""
        return self._retry(self._read_once, address)

    def _read_once(self, address: str) -> MeterReading:
        with self._lock:
            return self._read_locked(address)

    def _read_locked(self, address: str) -> MeterReading:
        self._init()
        if not self._select(address):
            raise MeterBusError(f"no clean answer selecting {address}")

        telegram = self._request(first=True)
        if not isinstance(telegram, meterbus.TelegramLong):
            raise MeterBusError(f"expected data, got {type(telegram).__name__}")

        # Meters that do not fit their data in one telegram set "more records
        # follow" and hand over the rest on subsequent requests. Skipping this
        # silently truncates such a meter to whatever fitted in frame one.
        guard = 0
        while telegram.more_records_follow:
            guard += 1
            if guard > _MAX_TELEGRAMS:
                raise MeterBusError(
                    f"{address} never stopped sending telegrams "
                    f"(gave up after {_MAX_TELEGRAMS})"
                )
            nxt = self._request(first=False)
            if not isinstance(nxt, meterbus.TelegramLong):
                break
            telegram = telegram + nxt

        return _interpret(telegram)

    def _retry(self, fn, *args):
        """Run ``fn``; on a link-level failure reopen the port and try once more.

        One retry, not a loop: a sweep of dozens of meters must not turn a dead
        bus into minutes of timeouts.
        """
        try:
            return fn(*args)
        except (MeterBusError, meterbus.MBusFrameDecodeError, OSError):
            if self._owns_port:
                self.close()
            return fn(*args)

    def read_meters(self, addresses: list[str]) -> dict[str, MeterReading]:
        """Read several meters in sequence.

        The bus is half-duplex with one master, so this is deliberately serial —
        there is no faster shape available. A meter that fails is omitted rather
        than failing the sweep, so one dead meter cannot black out the other
        forty.
        """
        out: dict[str, MeterReading] = {}
        for address in addresses:
            try:
                out[address] = self.read(address)
            except (MeterBusError, meterbus.MBusFrameDecodeError, OSError):
                continue
        return out

    # -- discovery -------------------------------------------------------

    def probe(self) -> None:
        """Raise unless something on this link behaves like an M-Bus.

        A full-wildcard probe that comes back as anything other than empty
        proves at least one meter is listening. Silence means a wrong port,
        wrong parity, no bus power, or nothing connected.
        """
        self._init()
        outcome, _ = self._probe(WILDCARD)
        if outcome is _EMPTY:
            raise MeterBusError("nothing answered on the bus")

    def scan(self, mask: str = WILDCARD, on_found=None, on_progress=None) -> list[str]:
        """Find every meter on the bus by wildcard tree search.

        Selecting with ``F`` wildcards addresses many meters at once. Narrowing
        one hex digit at a time wherever more than one answers turns that into a
        full inventory with no prior knowledge of the bus.

        Expect minutes, not seconds. ``on_found`` is called with each address as
        it is confirmed, and ``on_progress`` with ``(found_count, prefix)`` for
        each address prefix tried, so a caller can show that it is still moving.

        There is deliberately no percentage. Weighting by address space is the
        only measure available before the search runs, and it bears almost no
        relation to time: on a real bus three top-level prefixes held every
        meter and took all the time, while the other seven were empty and
        finished in seconds. Depth-first order then makes it worse — the walk
        starts inside the biggest branch, so it reads 0% while meters are
        visibly being found, then lurches. Honest weighting would need to know
        how much work each branch hides, which is what the scan exists to
        discover.
        """
        self._init()
        found: list[str] = []
        self._scan_mask(mask, found, on_found, on_progress, collided=True)
        return sorted(found)

    def _scan_mask(
        self,
        mask: str,
        found: list[str],
        on_found,
        on_progress,
        collided: bool,
    ) -> int:
        """Walk one prefix. Returns how many meters were found beneath it.

        ``collided`` says the caller already proved at least two meters live
        under this prefix, which lets an empty result be recognised as a lost
        probe rather than an empty branch (see the retry at the end).
        """
        try:
            pos = mask.index("F")
        except ValueError:
            # Fully specified. Reached only when two meters answer to the same
            # complete address, which cannot be narrowed any further.
            outcome, address = self._probe(mask)
            if outcome is _SINGLE and address:
                self._add(address, found, on_found)
            return len(found)

        # Positions 0-7 are the identification number, which is BCD — digits
        # A-F cannot occur there. Skipping them is a third of the probes, and a
        # probe that nothing answers costs a full timeout.
        digits = "0123456789" if pos < 8 else "0123456789ABCDEF"
        before = len(found)

        for digit in digits:
            candidate = mask[:pos] + digit + mask[pos + 1 :]
            if on_progress is not None:
                on_progress(len(found), candidate[: pos + 1])
            outcome, address = self._probe(candidate)
            if outcome is _COLLISION:
                # Two or more meters share this prefix — or one answered in a
                # way we could not read, which has to be treated the same. The
                # cost of narrowing a branch that turns out to hold one meter is
                # seconds; the cost of not narrowing a real collision is losing
                # every meter under it.
                self._scan_mask(candidate, found, on_found, on_progress, True)
                continue
            if outcome is _SINGLE and address is not None:
                self._add(address, found, on_found)

        if collided and len(found) == before:
            # Something answered this prefix a moment ago, yet none of its
            # children did. That is a lost probe, not an empty branch — and
            # silently pruning here is what loses whole families of meters at a
            # stroke. Go round once more, slower.
            _LOGGER.debug("scan: %s collided but yielded nothing; retrying", mask)
            return self._retry_mask(mask, found, on_found, on_progress)
        return len(found)

    def _retry_mask(self, mask: str, found: list[str], on_found, on_progress) -> int:
        """Re-walk a prefix with a longer timeout, once."""
        previous = self._select_timeout
        self._select_timeout = previous * _RETRY_TIMEOUT_FACTOR
        try:
            return self._scan_mask(mask, found, on_found, on_progress, collided=False)
        finally:
            self._select_timeout = previous

    def _add(self, address: str, found: list[str], on_found) -> None:
        """Record a meter, ignoring one already seen in another branch."""
        if address in found:
            return
        found.append(address)
        if on_found is not None:
            on_found(address)

    def _probe(self, mask: str) -> tuple[str, str | None]:
        """Classify one address mask as empty, a single meter, or a collision.

        The verdict comes from the **data frame**, not from the select's
        acknowledgement, and that is the whole point:

        * Every meter matching a select answers with the same single byte,
          ``0xE5``. Superimposed on the wire those often still read as a valid
          ``0xE5``, so "got an ACK" never meant "exactly one meter" — it meant
          "at least one". Believing it stops the search at a prefix that still
          holds a dozen meters.
        * The same superposition can also come out unreadable and be dropped
          entirely, which looks exactly like an empty branch and prunes it.
        * A data frame is ~100 bytes with a checksum. Two of them overlapping
          effectively never produce a valid frame, so it discriminates properly.

        Each probe starts with SND_NKE to deselect. Selection is sticky on
        M-Bus: without it, a select that failed leaves the *previous* meter
        selected and the request returns that meter instead — a stale answer
        that reads as a find in a branch it does not belong to.
        """
        with self._lock:
            return self._probe_locked(mask)

    def _probe_locked(self, mask: str) -> tuple[str, str | None]:
        self._deselect()
        self._timeout(self._select_timeout)
        try:
            self._send_select(mask)
            _, ack_bytes = self._read_reply()
            telegram, data_bytes = self._request_reply()
        except OSError as err:
            _LOGGER.debug("scan: probe %s failed: %s", mask, err)
            return _COLLISION, None
        finally:
            self._timeout(self._timeout_value)

        if isinstance(telegram, meterbus.TelegramLong):
            address = telegram.secondary_address.upper()
            if _matches(address, mask):
                return _SINGLE, address
            # Somebody answered, but not who we asked for. Do not trust it as a
            # find here; narrow instead.
            _LOGGER.debug("scan: %s answered a select for %s", address, mask)
            return _COLLISION, None

        if ack_bytes or data_bytes:
            self._settle()
            return _COLLISION, None
        return _EMPTY, None

    def _request_reply(self) -> tuple[Any, bool]:
        """Send a one-shot REQ_UD2 and read whatever comes back.

        No FCB: selection is sticky and the select frame carries an FCB of its
        own, so a request toggling another on top can read as a repeat and make
        a meter resend its previous reply.
        """
        frame = meterbus.TelegramShort()
        frame.header.cField.parts = [
            meterbus.CONTROL_MASK_REQ_UD2 | meterbus.CONTROL_MASK_DIR_M2S
        ]
        frame.header.aField.parts = [meterbus.ADDRESS_NETWORK_LAYER]
        meterbus.serial_send(self.connect(), frame)
        return self._read_reply()

    def _deselect(self) -> None:
        """SND_NKE to the network layer: drop any current selection."""
        self._flush()
        self._timeout(self._select_timeout)
        try:
            meterbus.send_ping_frame(self.connect(), meterbus.ADDRESS_NETWORK_LAYER)
            self._read_reply()
        except OSError:
            pass


def _main() -> None:
    """Dump the bus. Stop anything else holding the port first."""
    import argparse

    parser = argparse.ArgumentParser(description="Read an M-Bus.")
    parser.add_argument("device", help="serial port, or host:port of a TCP bridge")
    parser.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("-s", "--scan", action="store_true", help="scan for meters")
    parser.add_argument("-a", "--address", action="append", help="address to read")
    parser.add_argument(
        "-m",
        "--mask",
        default=WILDCARD,
        help="secondary address mask to scan, F is a wildcard "
        "(narrowing it is the only way to make a scan quick)",
    )
    args = parser.parse_args()

    if ":" in args.device and not args.device.startswith("/"):
        host, _, tcp = args.device.partition(":")
        bus = MeterBus(host=host, tcp_port=int(tcp), baud=args.baud)
    else:
        bus = MeterBus(serial_port=args.device, baud=args.baud)

    addresses = args.address or []
    if args.scan or not addresses:
        print(f"scanning {args.device} at {args.baud} 8E1 (this takes minutes) ...")
        started = time.monotonic()
        # flush: a scan runs for minutes, and a progress line nobody sees until
        # the end is not a progress line.
        addresses = bus.scan(
            args.mask, on_found=lambda a: print(f"  found {a}", flush=True)
        )
        print(f"{len(addresses)} meters in {time.monotonic() - started:.0f}s\n")

    for address, reading in bus.read_meters(addresses).items():
        print(
            f"{address}  {reading.manufacturer} v{reading.version} "
            f"{reading.medium_name}  status 0x{reading.status:02X}"
        )
        for rec in reading.records:
            print(
                f"    {rec.vif:34s} {rec.unit:22s}"
                f" st={rec.storage} tar={rec.tariff} dev={rec.device}"
                f" {rec.function.split('.')[-1]:20s} {rec.value}"
            )
    bus.close()


if __name__ == "__main__":
    _main()
