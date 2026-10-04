"""Offline tests against frames captured from real meters.

The original capture fixtures came off a live M-Bus byte for byte; additional
synthetic frames are explicitly labelled below.
This tests both captured replies and targeted protocol edge cases. Needs
pyMeterBus and nothing else — no Home Assistant, no serial port, no meters::

    python3 tests/test_frames.py
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

# Load the integration's Home-Assistant-free modules without importing the
# package itself (its __init__ pulls in homeassistant).
_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "meterbus"
_pkg = types.ModuleType("mb")
_pkg.__path__ = [str(_PKG)]  # type: ignore[attr-defined]
sys.modules["mb"] = _pkg


def _load(name):
    spec = importlib.util.spec_from_file_location(f"mb.{name}", _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"mb.{name}"] = module
    spec.loader.exec_module(module)
    return module


api = _load("api")
profiles = _load("profiles")

import meterbus

# Apator water meter, 2400 baud, secondary addressing. 12 records.
APA_WATER = bytes.fromhex(
    "68 56 56 68 08 00 72 09 28 14 00 01 06 03 07 19 FA 00 00 0C 78 09 28"
    " 14 00 04 6D 05 2E 47 38 04 13 A1 6D 01 00 02 3B 00 00 44 13 FF 6B 01"
    " 00 42 6C 41 38 02 27 9D 0C 03 FD 17 1E 0B 00 04 FF 0A 02 01 04 00 02"
    " FF 0B 9F 9C 03 FF 0C 12 00 C1 0F 00 01 09 24 FF 00 02 02 01 00 AA 16"
)

# Siemens WFZ51 M-Bus module on a WFx5.. heat meter. 13 records, the last one
# a manufacturer-specific blob after DIF 0x0F.
LSE_HEAT = bytes.fromhex(
    "68 61 61 68 08 00 72 33 67 32 66 65 32 29 04 52 00 00 00 0C 05 57 28"
    " 19 00 0C 13 84 23 37 02 04 6D 1E 0D 47 38 0B 22 12 05 08 32 6C FF FF"
    " 0C 78 94 08 07 56 06 FD 0C 14 00 01 00 10 02 0D FD 0B 05 31 35 5A 46"
    " 57 0C FD 10 94 08 07 56 32 FD 17 00 00 4C 05 10 92 17 00 42 6C 3F 3C"
    " 0F 02 7A 25 00 02 78 25 00 71 16"
)

# Siemens electricity meter. Nine energy records: three tariffs times
# instantaneous / maximum / minimum.
SIE_ELECTRICITY = bytes.fromhex(
    "68 4B 4B 68 08 00 72 85 01 01 18 25 CD 01 02 9F 00 00 00 0C 04 02 00"
    " 06 01 8C 10 04 02 00 06 01 8C 20 04 00 00 00 00 1C 04 02 00 06 01 9C"
    " 10 04 02 00 06 01 9C 20 04 00 00 00 00 2C 04 00 00 00 00 AC 10 04 00"
    " 00 00 00 AC 20 04 00 00 00 00 81 16"
)


def _keys(reading):
    """Sensor key -> SensorPoint for one reading."""
    return {p.key: p for p in profiles.sensors_for(reading)}


def _close(got, want, tol=1e-6):
    return abs(got - want) <= tol * max(1.0, abs(want))


def test_apa_water():
    """A water meter decodes to volume, flow, runtime and a frozen snapshot."""
    reading = api.parse_frame(APA_WATER)
    assert reading.address == "0014280901060307", reading.address
    assert reading.manufacturer == "APA", reading.manufacturer
    assert reading.medium_name == "Water", reading.medium_name
    assert reading.identification == "00142809", reading.identification
    assert len(reading.records) == 12, len(reading.records)

    points = _keys(reading)
    # Volume arrives already scaled by the VIF exponent: the wire carries
    # 93601 in units of milli-m^3.
    assert _close(points["volume"].value, 93.601), points["volume"].value
    assert points["volume"].sensor.device_class == "water"
    assert points["volume"].sensor.state_class == "total_increasing"
    assert points["volume"].sensor.unit == "m³"

    assert _close(points["volume_flow"].value, 0.0)
    assert points["volume_flow"].sensor.state_class == "measurement"

    # 3229 days of operating time, normalised to seconds by pyMeterBus.
    assert _close(points["operating_time"].value, 3229 * 86400.0)
    assert points["operating_time"].sensor.diagnostic

    # Fabrication number, the meter's clock and three manufacturer-specific
    # integers carry no row in the table, so they produce no entities.
    for absent in ("fabrication_no", "date_time", "manufacturer_spec"):
        assert absent not in points, absent


def test_apa_snapshot_is_not_a_counter():
    """A billing-date snapshot must never be TOTAL_INCREASING.

    Storage 1 is a value the meter froze at the end of a period. It steps
    backwards when the period rolls over, which a total-increasing sensor would
    report to the energy dashboard as an enormous phantom delta.
    """
    points = _keys(api.parse_frame(APA_WATER))
    snapshot = points["volume_prev1"]
    assert _close(snapshot.value, 93.183), snapshot.value
    assert snapshot.sensor.state_class is None
    assert snapshot.enabled is False


def test_heat_meter_volume_is_not_water_consumption():
    """A calorimeter's m³ is a circulating loop, not water anyone used.

    Presented as device_class WATER it becomes eligible for the Energy
    dashboard's Water section, where it would inflate household water use by
    the whole heating circulation. Home Assistant's own landisgyr_heat_meter
    models the same figure as VOLUME.
    """
    heat = _keys(api.parse_frame(LSE_HEAT))["volume"]
    water = _keys(api.parse_frame(APA_WATER))["volume"]
    assert heat.sensor.device_class == "volume", heat.sensor.device_class
    assert water.sensor.device_class == "water", water.sensor.device_class
    # Both are still cumulative counters, and both still read in m³.
    assert heat.sensor.state_class == water.sensor.state_class == "total_increasing"
    assert heat.sensor.unit == water.sensor.unit == "m³"


def test_thermal_energy_is_distinguishable_from_electrical():
    """Both are device_class energy in kWh, so the translation key separates them.

    Not an icon set in code: icon translations are static per translation key,
    so thermal energy needs its own key to carry a flame — which also gets it
    the better name.
    """
    heat = _keys(api.parse_frame(LSE_HEAT))["energy"]
    electric = _keys(api.parse_frame(SIE_ELECTRICITY))["energy"]
    assert heat.sensor.device_class == electric.sensor.device_class == "energy"
    assert heat.translation_key == "heat_energy", heat.translation_key
    assert electric.translation_key == "energy", electric.translation_key
    # The unique_id half must not move, or existing entities lose their history.
    assert heat.key == electric.key == "energy"


def test_lse_heat():
    """A heat meter decodes to energy in kWh plus volume and runtime."""
    reading = api.parse_frame(LSE_HEAT)
    assert reading.address == "6632673365322904", reading.address
    assert reading.manufacturer == "LSE", reading.manufacturer
    assert reading.medium_name == "Heat", reading.medium_name

    points = _keys(reading)
    # 192857 * 100 Wh = 19 285 700 Wh, reported as kWh.
    assert _close(points["energy"].value, 19285.7), points["energy"].value
    assert points["energy"].sensor.device_class == "energy"
    assert points["energy"].sensor.state_class == "total_increasing"
    assert points["energy"].sensor.unit == "kWh"

    assert _close(points["volume"].value, 2372.384)
    assert _close(points["on_time"].value, 80512 * 3600.0), points["on_time"].value
    assert _close(points["energy_prev1"].value, 17921.0)
    assert points["energy_prev1"].sensor.state_class is None

    # The WFZ51 reports its error flags as an error-state record rather than an
    # instantaneous one, so the function filter drops it. Its model string,
    # parameter-set id ("WFZ51") and customer location are not numbers and have
    # no rows either.
    assert "error_flags" not in points
    assert "model_version" not in points


def test_sie_electricity_tariffs():
    """Tariff registers become their own sensors; max/min registers do not."""
    reading = api.parse_frame(SIE_ELECTRICITY)
    assert reading.address == "1801018525CD0102", reading.address
    assert reading.manufacturer == "SIE", reading.manufacturer
    assert reading.medium_name == "Electricity", reading.medium_name
    assert len(reading.records) == 9, len(reading.records)

    points = _keys(reading)
    # Nine records in, three sensors out: the maximum- and minimum-value copies
    # of every register are dropped by function, not by an address list.
    assert set(points) == {"energy", "energy_t1", "energy_t2"}, sorted(points)
    assert _close(points["energy"].value, 10600.02), points["energy"].value
    assert _close(points["energy_t1"].value, 10600.02)
    assert _close(points["energy_t2"].value, 0.0)
    assert points["energy_t1"].translation_key == "energy_tariff"
    assert points["energy_t1"].placeholders == {"tariff": "1"}


def test_error_flags_are_diagnostic_and_off_by_default():
    """The Apator meters do report error flags as an instantaneous record."""
    points = _keys(api.parse_frame(APA_WATER))
    flags = points["error_flags"]
    assert _close(flags.value, 2846.0), flags.value
    assert flags.sensor.diagnostic is True
    assert flags.enabled is False
    assert flags.sensor.state_class is None


class _FakePort:
    """A serial port that answers each written frame with a canned reply."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.written = []
        self._rx = bytearray()

    def write(self, data):
        self.written.append(bytes(data))
        if self.replies:
            self._rx += self.replies.pop(0)

    def read(self, count=1):
        chunk = bytes(self._rx[:count])
        del self._rx[:count]
        return chunk

    def close(self):
        pass


_ACK = b"\xe5"


def test_read_selects_then_requests():
    """A read is SND_NKE, then select by secondary address, then REQ_UD2."""
    port = _FakePort([_ACK, _ACK, LSE_HEAT])
    bus = api.MeterBus(port=port)
    reading = bus.read("6632673365322904")
    assert reading.address == "6632673365322904"

    assert len(port.written) == 3, port.written
    # SND_NKE to the network layer address, which resets any half-finished
    # multi-telegram conversation a previous master left behind.
    assert port.written[0][1] == meterbus.CONTROL_MASK_SND_NKE
    assert port.written[0][2] == meterbus.ADDRESS_NETWORK_LAYER
    # The select carries the secondary address, least significant byte first.
    assert bytes.fromhex("33673266") in port.written[1]
    # REQ_UD2 to the network layer, FCB set on the first request of a sequence.
    assert port.written[2][2] == meterbus.ADDRESS_NETWORK_LAYER
    assert port.written[2][1] & meterbus.CONTROL_MASK_FCB


def test_fcb_toggles_between_telegrams():
    """Consecutive requests must alternate FCB, or a meter repeats itself.

    A meter uses the toggle to tell "send me the next telegram" apart from
    "I missed your last reply, send it again". Never toggling makes a
    multi-telegram meter return frame one forever.
    """
    port = _FakePort([LSE_HEAT, LSE_HEAT, LSE_HEAT])
    bus = api.MeterBus(port=port)
    bus._request(first=True)
    bus._request(first=False)
    bus._request(first=False)
    fcb = [bool(frame[1] & meterbus.CONTROL_MASK_FCB) for frame in port.written]
    assert fcb == [True, False, True], fcb


def test_unreachable_meter_is_skipped_not_fatal():
    """One silent meter must not lose the readings of the others."""
    port = _FakePort([_ACK, _ACK, LSE_HEAT])
    bus = api.MeterBus(port=port)
    data = bus.read_meters(["6632673365322904", "0014280901060307"])
    assert list(data) == ["6632673365322904"], list(data)


def _probe_bus(replies):
    """A bus whose probe sequence (deselect, select, request) gets ``replies``."""
    bus = api.MeterBus(port=_FakePort(replies))
    return bus


def test_probe_empty_branch():
    """Nothing at all on the wire means no meter has this prefix."""
    outcome, address = _probe_bus([b"", b"", b""])._probe("001428FFFFFFFFFF")
    assert outcome == api._EMPTY, outcome
    assert address is None


def test_probe_single_meter():
    """A clean data frame whose address fits the mask is one meter."""
    outcome, address = _probe_bus([_ACK, _ACK, APA_WATER])._probe("001428FFFFFFFFFF")
    assert outcome == api._SINGLE, outcome
    assert address == "0014280901060307", address


def test_collision_is_not_read_as_a_single_meter():
    """A clean ACK followed by an unreadable data frame is a collision.

    This is the case that used to lose meters wholesale. Every meter matching a
    select answers with the same byte, 0xE5, so colliding acknowledgements often
    still decode as a valid ACK — believing it stops the search at a prefix that
    may still hold a dozen meters. Only the data frame settles it.
    """
    garbage = bytes.fromhex("68 20 20 68 08 00 72 de ad be ef 16")
    outcome, address = _probe_bus([_ACK, _ACK, garbage])._probe("001428FFFFFFFFFF")
    assert outcome == api._COLLISION, outcome
    assert address is None


def test_silent_ack_with_noisy_data_is_a_collision():
    """A lost ACK does not make a branch empty if anything answered later.

    Superimposed acknowledgements can come out unreadable and be dropped
    entirely. Treating that as an empty branch prunes it, which is how a whole
    family of meters disappears from one bad probe.
    """
    outcome, _ = _probe_bus([b"", b"", b"\x00\x00"])._probe("18FFFFFFFFFFFFFF")
    assert outcome == api._COLLISION, outcome


def test_probe_rejects_a_meter_outside_the_mask():
    """A stale answer from a previously selected meter is not a find here."""
    outcome, address = _probe_bus([_ACK, _ACK, APA_WATER])._probe("6632FFFFFFFFFFFF")
    assert outcome == api._COLLISION, outcome
    assert address is None


def test_scan_reports_prefixes_it_is_trying():
    """Progress is the prefix under way plus the count, never a percentage.

    A percentage would have to be weighted by address space, which bears almost
    no relation to time — one prefix on the development bus holds 24 of 41
    meters — so it reads 0% while meters are visibly being found, then lurches.
    """
    bus = api.MeterBus(port=_FakePort([]))
    seen = []
    result = bus.scan(on_progress=lambda n, prefix: seen.append((n, prefix)))
    assert result == [], result
    assert seen, "no progress was reported"
    # Every report names a prefix that could really be tried next, and the
    # counts never go backwards.
    prefixes = [p for _, p in seen]
    assert prefixes[:3] == ["0", "1", "2"], prefixes[:3]
    assert all(set(p) <= set("0123456789ABCDEF") for p in prefixes)
    counts = [n for n, _ in seen]
    assert counts == sorted(counts), counts


def test_scan_retries_a_prefix_that_collided_but_yielded_nothing():
    """A collision whose children all read empty is a lost probe, not an absence.

    Without the retry, one bad probe at a shallow prefix silently discards every
    meter beneath it — which is exactly how a whole family of meters vanished
    from a real scan.
    """
    bus = api.MeterBus(port=_FakePort([]))
    walked = []
    original = bus._probe

    def fake_probe(mask):
        walked.append(mask)
        return (api._COLLISION, None) if mask == api.WILDCARD else (api._EMPTY, None)

    bus._probe = fake_probe
    bus.scan()
    bus._probe = original
    # 10 BCD digits at position 0, walked twice: once normally, once on retry.
    first_digit_probes = [m for m in walked if m.startswith("0") and m[1] == "F"]
    assert len(first_digit_probes) == 2, first_digit_probes


def test_transactions_do_not_interleave():
    """A sweep and a scan must take turns, not overlap mid-conversation.

    The bus has one master and no arbitration, and both the polling coordinator
    and a discovery scan want it. Interleaving two conversations corrupts both:
    selection is sticky, so a read that gets its select answered by the other
    party's request reads the wrong meter.
    """
    import threading

    bus = api.MeterBus(port=_FakePort([]))
    overlaps = []
    inside = []
    real_probe = bus._probe_locked

    def watched(mask):
        inside.append(1)
        if len(inside) > 1:
            overlaps.append(mask)
        try:
            return real_probe(mask)
        finally:
            inside.pop()

    bus._probe_locked = watched
    threads = [threading.Thread(target=bus._probe, args=("FFFFFFFFFFFFFFFF",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps == [], overlaps


def test_every_entity_name_and_icon_is_translated():
    """Every name an entity can ask for must exist, with matching placeholders.

    Names and icons are looked up by translation key at runtime, so a row added
    to the profile table without its strings.json entry produces an entity with
    no name and an error in the log — and a placeholder the translation does
    not use (or vice versa) fails when Home Assistant formats it. Neither shows
    up until that particular meter is on someone's bus, which is exactly the
    kind of bug that ships.
    """
    import json
    import re

    names = json.loads((_PKG / "strings.json").read_text())["entity"]["sensor"]
    icons = json.loads((_PKG / "icons.json").read_text())["entity"]["sensor"]

    required = set()
    for sensor in profiles.SENSORS.values():
        # Both as-is and as a heat meter would present it.
        for variant in (sensor, profiles._for_medium(sensor, 0x04)):
            for storage, tariff in ((0, None), (0, 1), (1, None), (1, 1)):
                for device in (0, 1, 2):
                    _, key, placeholders, _ = profiles._decorate(
                        variant, storage, tariff, device
                    )
                    required.add((key, tuple(sorted(placeholders))))

    missing = {key for key, _ in required} - set(names)
    assert not missing, f"no name in strings.json for: {sorted(missing)}"

    for key, placeholders in sorted(required):
        used = tuple(sorted(set(re.findall(r"{(\w+)}", names[key]["name"]))))
        assert used == placeholders, f"{key}: name uses {used}, entity gives {placeholders}"

    stray = set(icons) - set(names)
    assert not stray, f"icons.json names keys that do not exist: {sorted(stray)}"


def test_address_list_parsing():
    """The meter list is pasted from other tools, so be liberal about format."""
    valid, bad = api.parse_addresses(
        "0014280901060307\n6632673365322904, 1801018525cd0102 ;\n"
    )
    assert valid == [
        "0014280901060307",
        "1801018525CD0102",
        "6632673365322904",
    ], valid
    assert bad == [], bad

    # Wrong length, non-hex and a stray primary address must be rejected
    # loudly rather than quietly dropped — a mistyped meter is a meter that
    # silently never appears.
    valid, bad = api.parse_addresses("0014280901060307 12345 nonsense 0x14")
    assert valid == ["0014280901060307"], valid
    assert bad == ["12345", "NONSENSE", "0X14"], bad


def test_mask_matching():
    """F in a mask is a wildcard; everything else must match exactly."""
    assert api._matches("0014280901060307", "001428FFFFFFFFFF")
    assert api._matches("0014280901060307", "0014280901060307")
    assert not api._matches("0014280901060307", "001429FFFFFFFFFF")
    assert not api._matches("0014280901060307", "00142809010603")



def _synthetic_heat_channels(serial=1, extra_records=b""):
    """Synthetic HYD v43 reply; reported scales/channels, anonymized serial.

    This is not a raw capture. The user supplied decoded records with a main
    0.0001 m³ register and two 0.001 m³ pulse-input registers (devices 1/2).
    Device 2 needs two DIFEs, which also tests higher device bits.
    """
    header = bytes([0x08, 0, 0x72, serial, 0, 0, 0, 0x24, 0x23, 0x2B, 4, 0, 0, 0, 0])
    records = (
        bytes([0x04, 0x12]) + (28346507).to_bytes(4, "little")
        + bytes([0x84, 0x40, 0x13]) + (194530).to_bytes(4, "little")
        + bytes([0x84, 0x80, 0x40, 0x13]) + (714701).to_bytes(4, "little")
        + extra_records
    )
    body = header + records
    return bytes([0x68, len(body), len(body), 0x68]) + body + bytes([sum(body) % 256, 0x16])


def test_additional_device_channels_survive_decoding():
    """Equal quantity/storage/tariff must not collapse different devices."""
    reading = api.parse_frame(_synthetic_heat_channels())
    assert [r.device for r in reading.records] == [0, 1, 2]
    points = _keys(reading)
    assert list(points) == ["volume", "volume_device_1", "volume_device_2"]
    for key, expected in (("volume", 2834.6507), ("volume_device_1", 194.530),
                          ("volume_device_2", 714.701)):
        point = points[key]
        assert _close(point.value, expected), (key, point.value)
        assert point.unit == "m³"
        assert point.sensor.state_class == "total_increasing"
        assert point.enabled
    assert points["volume"].translation_key == "circuit_volume"
    for device in (1, 2):
        point = points[f"volume_device_{device}"]
        assert point.translation_key == "volume_device"
        assert point.placeholders == {"device": str(device)}
        # The input number alone does not establish water consumption.
        assert point.sensor.device_class == "volume"


def test_device_channels_remain_separate_on_multiple_meters():
    """The same channels on multiple meter addresses retain distinct identities."""
    readings = [api.parse_frame(_synthetic_heat_channels(i)) for i in (1, 2, 3)]
    identifiers = {
        (reading.address, point.key)
        for reading in readings for point in profiles.sensors_for(reading)
    }
    assert len(identifiers) == 9
    for reading in readings:
        assert len(profiles.values_for(reading)) == 3


def test_device_channel_tariff_and_snapshot():
    """Device qualifiers compose with tariff/storage without enabling snapshots."""
    # DIF 0xC4: storage 1. DIFE 0x50: device 1 and tariff 1.
    extra = bytes([0xC4, 0x50, 0x13]) + (180000).to_bytes(4, "little")
    points = _keys(api.parse_frame(_synthetic_heat_channels(extra_records=extra)))
    point = points["volume_t1_prev1_device_1"]
    assert _close(point.value, 180)
    assert not point.enabled
    assert point.sensor.state_class is None
    assert point.translation_key == "volume_tariff_previous_device"
    assert point.placeholders == {"tariff": "1", "period": "1", "device": "1"}


def test_repeated_device_record_is_still_deduplicated():
    extra = bytes([0x84, 0x40, 0x13]) + (194530).to_bytes(4, "little")
    reading = api.parse_frame(_synthetic_heat_channels(extra_records=extra))
    assert len(reading.records) == 4
    assert len(profiles.sensors_for(reading)) == 3


def test_explicit_device_zero_preserves_existing_identity():
    # DIFE explicitly specifies zero; it must match an omitted device number.
    extra = bytes([0x84, 0, 0x12]) + (28346507).to_bytes(4, "little")
    reading = api.parse_frame(_synthetic_heat_channels(extra_records=extra))
    assert reading.records[-1].device == 0
    assert list(_keys(reading)) == ["volume", "volume_device_1", "volume_device_2"]


def _main():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _main()
