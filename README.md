<img src="custom_components/meterbus/brand/icon.png" width="96" align="right" alt="M-Bus">

# Meter-Bus — Home Assistant integration

[![HACS: custom](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Validate](https://github.com/rpodgorny/hass-meterbus/actions/workflows/validate.yml/badge.svg)](https://github.com/rpodgorny/hass-meterbus/actions/workflows/validate.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

Local polling integration for **wired M-Bus** (Meter-Bus, EN 13757-2/3) — the
serial bus that heat meters, water meters and electricity meters in apartment
buildings are read out over.

Point it at an M-Bus master, and every meter on the bus becomes a Home
Assistant device with sensors that land straight on the **Energy dashboard**.
No addresses, no YAML.

> **Wired, not wireless.** Searching for "M-Bus" and Home Assistant mostly
> turns up *wireless* M-Bus (wM-Bus) and [wmbusmeters]. Different radio,
> different problem. This is the two-wire bus you can trip over.

[wmbusmeters]: https://github.com/wmbusmeters/wmbusmeters

Developed against a bus of **41 meters** on one 2400-baud segment — 22 Apator
v3 and 2 Apator v21 water meters, 8 Siemens WFZ51 heat meters, 8 Siemens and 1
Landis electricity meter. All 41 read in one 29.5-second sweep.

## Why one integration and not one per meter brand

M-Bus records are **self-describing**. A record carries its own DIF/VIF, which
say what the value is, in what unit, at what scale, for which tariff and
storage period. Three brands from three vendors on the bus here all answer with
the same structure, and the same decoder reads all of them.

So there is no per-brand code. There is one table in
[`profiles.py`](custom_components/meterbus/profiles.py) keyed on what a record
says it is:

```python
("VIFUnit.VOLUME", "MeasureUnit.M3"): Sensor(
    key="volume", name="Volume", unit="m³",
    device_class="water", state_class="total_increasing", precision=3,
),
```

Records with no row are skipped, which is what silently drops fabrication
numbers, model strings, parameter-set identifiers and manufacturer blobs
without a single special case. Supporting a new meter is adding rows.

Tariff and storage number are **qualifiers on a row**, not rows of their own —
the nine energy registers a Siemens electricity meter reports collapse to one
table entry.

## Install

**HACS** → ⋮ → *Custom repositories* → add `https://github.com/rpodgorny/hass-meterbus`
as an **Integration** → install → restart Home Assistant.

Or copy `custom_components/meterbus/` into your `config/custom_components/`.

Then **Settings → Devices & services → Add integration → Meter-Bus**.

## Setup

M-Bus meters do not drive the bus themselves — they need a **level converter**
(an "M-Bus master") that supplies the bus voltage and turns it into a serial
port. USB ones are cheap and are what this was developed against.

The master reaches Home Assistant one of two ways:

**On a serial port** of the Home Assistant machine — give it the device path.
This is the simple case: plug the USB M-Bus master into the box running Home
Assistant.

**Over the network** — a master on any machine, published as a raw TCP port.
That machine does not need Home Assistant on it; a Raspberry Pi next to the
meters is enough:

```yaml
# /etc/ser2net.yaml
connection: &mbus
  accepter: tcp,10001
  connector: serialdev,/dev/ttyUSB0,2400e81,local
  options:
    kickolduser: true
```

Note **`2400e81`** — 2400 baud, **even** parity, 8 data bits, 1 stop bit. Even
parity is not a preference: a meter will not answer at all without it, which
looks exactly like a dead bus. `socat` works the same way, as do hardware
serial-to-WiFi gateways.

Either way the config flow probes the link, scans the bus, then **shows you the
meter list to check before saving**.

### How the scan works

Meters are found by **wildcard tree search**: select an address mask with `F`
wildcards, see whether nobody, one meter or several answer, and narrow one hex
digit at a time wherever more than one does. It needs no prior knowledge of
what is on the bus. Measured on the development bus: **41 of 41 meters in 9.6
minutes**, no misses and no phantoms.

The subtle part is deciding *how many* meters answered a mask, and the obvious
signal is a trap:

- **Every matching meter acknowledges with the same byte, `0xE5`.** Superimposed
  on the wire those often still read as a perfectly valid `0xE5` — so "an ACK
  came back" means "at least one", never "exactly one". Treating it as "exactly
  one" stops the search at a prefix still holding a dozen meters, and they are
  lost silently.
- **The same superposition can also come out unreadable and be dropped**, which
  looks exactly like an empty branch and prunes it. On this bus that lost all
  nine electricity meters at once, from a single bad probe at prefix `1`.
- **Selection is sticky.** A select that fails leaves the *previous* meter
  selected, so the next request answers as that meter — a stale reply that
  reads as a find in a branch it does not belong to.

So the verdict comes from the **data frame**, not the acknowledgement. Each
probe is `SND_NKE` (deselect, so nothing stale can answer) → select → `REQ_UD2`,
and a reply is only "one meter" if a complete frame passes its checksum *and*
its address fits the mask. Two overlapping ~100-byte frames effectively never
do both. Anything else that made a sound is a collision and gets narrowed;
only true silence prunes.

One more safety net: if a prefix that just collided yields no meters at all
from any of its children, that is a contradiction — something answered a moment
ago. Rather than prune, the walk redoes that prefix once with longer timeouts.

The scan runs only when you add or reconfigure the bus, never on a restart.

The result is still shown in an **editable box** before saving. Not because it
is expected to be wrong, but because if your bus does turn up something this
does not handle, "type the addresses in" beats "the integration cannot see my
meters".

To pick up a meter added later: device page → ⋮ → **Reconfigure**. A rescan
only ever *adds* — a meter that did not answer during one scan is far more
likely to be briefly unreachable than gone, and dropping it would take its
history with it. Delete its device to be rid of a meter for good.

### Defaults

| Setting | Default | Note |
|---|---|---|
| Baud | 2400 | 8E1 always |
| TCP port | 10001 | ser2net's usual first port |
| Poll interval | 300 s | Settings → the integration → *Configure* |

A sweep costs roughly **0.7 s of bus time per meter** at 2400 baud — measured
at **29.5 s for all 41 meters** — and the bus is half-duplex, so that time is
serial. These are cumulative counters that move in hours; there is nothing to
gain from polling harder.

## Entities

One device per meter, named after its medium and serial (`Water 00142809`),
identified by its 16-digit secondary address.

| Record | Entity | Class |
|---|---|---|
| Energy (Wh / J) | `kWh` | `energy` / `total_increasing` |
| Volume, water meter | `m³` | `water` / `total_increasing` |
| Volume, heat/cooling meter | `m³` | `volume` / `total_increasing` |
| Volume flow | `m³/h` | `volume_flow_rate` / `measurement` |
| Power | `W` | `power` / `measurement` |
| Flow / return / external temperature | `°C` | `temperature` / `measurement` |
| Temperature difference | `K` | `temperature` / `measurement` |
| Mass | `kg` | `weight` / `total_increasing` |
| Operating time, on time | `s` | `duration`, diagnostic |
| Error flags | — | diagnostic, disabled by default |

Energy and volume are scaled to Home Assistant's base units — the wire carries
"100 Wh" and "milli-m³", nobody wants those on a dashboard — so the sensors
drop straight into the Energy dashboard as water and energy sources.

### Getting a useful view of it

Forty meters is a wall of devices and entities on the integration page, and no
amount of integration-side polish changes that — Home Assistant has no "meter"
entity domain to make them into something prettier. There are 45 entity
platforms and none of them is one; a meter has no behaviour to model, only a
number that goes up, so `sensor` plus a device class *is* the model.

What plays the role you are looking for is the **Energy dashboard**
(Settings → Dashboards → Energy). That is Home Assistant's meter-shaped UI —
consumption per period, comparison, cost — and these sensors are built for it:

| Meter | Where it goes |
|---|---|
| Water meters | **Water** section, directly |
| Electricity meters | **Grid consumption**, or *Individual devices* per flat |
| Calorimeters (kWh) | No thermal section exists. The [heat meter docs](https://www.home-assistant.io/integrations/landisgyr_heat_meter/) suggest adding heat as **Gas**, which the dashboard converts to kWh; [this discussion](https://github.com/orgs/home-assistant/discussions/2917) tracks native support |

For per-month or per-tariff figures, layer the core
[`utility_meter`](https://www.home-assistant.io/integrations/utility_meter/)
helper on any of these sensors — it does cycles and tariffs, so this
integration does not need to.

Then assign meters to **areas** (by flat or riser) and the device list stops
being one flat wall.

### Meters with additional device channels

The M-Bus device number distinguishes additional measurement channels within
one meter. For example, a heat meter can also report two external pulse-input
volumes. Nonzero channels get separate sensors named `Volume device 1`,
`Volume device 2`, etc.; tariff and billing-period variants stay separate too.
The main channel keeps its existing entity identifiers and history.

Channel numbers do not specify what is connected. Rename the auxiliary sensors
in Home Assistant to match the installation (for example, hot and cold water).
Auxiliary volumes on heat meters use the generic volume class; assigning water
consumption semantics requires knowing what each input actually measures.

### What is deliberately not exposed

- **Maximum and minimum registers.** The Siemens electricity meters report
  every energy register three times: instantaneous, maximum and minimum. Here
  the maxima read identical to the totals and the minima are all zero, so they
  are nine records carrying three values. Filtered out by function.
- **Storage number > 0 is disabled by default.** These are billing-date
  snapshots the meter froze. They step *backwards* when a period rolls over,
  so they ship with no state class — a `total_increasing` sensor would report
  a huge phantom delta to the energy dashboard. Enable them if you want the
  previous period's reading.
- **Fabrication numbers, model strings, parameter-set ids, customer locations
  and manufacturer-specific blobs.** Not measurements.

### Availability

A meter that does not answer a sweep goes *unavailable*; the others are
unaffected. Only if **no** meter answers does the whole integration go down,
because that is the bus or the master, not the meters.

## Naming your meters

Devices are named from the bus (`Heat 66326733`), because that is all the bus
knows. Rename them in Home Assistant — the device name is yours, and renaming
a device offers to rename its entities with it.

There is no import path for an existing address→description mapping. With 41
meters that is a real chore exactly once; automating it would need a
websocket-API script that outlives its usefulness the moment it finishes.

## Limitations

- **One master at a time.** M-Bus has no arbitration. Stop `libmbus` tools,
  other pollers or a second Home Assistant before this one runs.

  Inside the integration this is handled for you: there is one bus object per
  config entry, holding the port, and every transaction takes a lock. A
  reconfigure scan reuses that same object rather than opening the port a
  second time — which does not work anyway, since a serial device opened twice
  gets two masters garbling each other and a serial-to-TCP bridge is usually
  single-client, leaving the second connection unserved and every probe timing
  out. A scan and a polling sweep interleave one transaction at a time.
- **Secondary addressing only.** Primary addresses are settable, usually left
  at zero on every meter in a building, and say nothing about which meter is
  which. The secondary address is the meter's identity and is printed on it.
- **No writes.** This reads meters. Setting a meter's primary address, baud
  rate or clock is a job for a commissioning tool, and getting it wrong on a
  billing meter is not a thing to do from a dashboard.
- **Temperatures depend on the meter.** Flow/return temperature are standard
  VIFs and are mapped, but plenty of heat meters — including the Siemens
  WFZ51 here — do not put them in their default readout at all.

## Development

`custom_components/meterbus/api.py` imports no Home Assistant, so it runs
standalone as a bus dump tool:

```bash
pip install pyMeterBus pyserial
python3 custom_components/meterbus/api.py /dev/ttyUSB0            # scan, then read
python3 custom_components/meterbus/api.py 192.168.11.9:10001
python3 custom_components/meterbus/api.py -a 0014280901060307 /dev/ttyUSB0
python3 custom_components/meterbus/api.py -s -m 001428FFFFFFFFFF /dev/ttyUSB0
```

`-m` narrows the scan mask, which is the only way to make a scan quick — and a
handy way to hunt for one meter you know the start of.

`tests/test_frames.py` decodes **telegrams captured from the real meters**,
byte for byte, and drives the master against a fake port. No hardware, no Home
Assistant, no meters:

```bash
pip install pyMeterBus
python3 tests/test_frames.py
```

It pins the things that are expensive to get wrong: that VIF scaling lands on
kWh and m³, that billing snapshots never become `total_increasing`, that
tariff registers get their own sensors while max/min registers are dropped,
that a read is SND_NKE → select → REQ_UD2, that FCB alternates between
telegrams (without which a multi-telegram meter repeats frame one forever),
and that one silent meter does not lose the rest of the sweep.

### Why pyMeterBus and not libmbus

[libmbus] is the usual answer and it works, but it is a C library with
command-line tools — nothing a HACS integration can ship into a Home Assistant
OS container. [pyMeterBus] is pure Python, on PyPI, and applies the VIF
exponent for you.

It is also more accurate on this hardware: libmbus's XML labels every record of
the Siemens electricity meters `Instantaneous value`, including the ones whose
DIF says `MAXIMUM_VALUE` and `MINIMUM_VALUE`. pyMeterBus reads the DIF, which
is what makes dropping those registers possible at all.

[libmbus]: https://github.com/rscada/libmbus
[pyMeterBus]: https://github.com/ganehag/pyMeterBus

## Licence

The integration — client, coordinator, config flow, profile table and the
sensor platform — is original work in this repository, licensed under
**GPL-3.0** (see [LICENSE](LICENSE)).

The artwork in `custom_components/meterbus/brand/` is **not**. It is the M-Bus
mark, owned by the M-Bus Usergroup and used descriptively to identify the
protocol this integration speaks, under the terms set out in
[`brand/ATTRIBUTION.md`](custom_components/meterbus/brand/ATTRIBUTION.md). Since
[Home Assistant 2026.3](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api/)
these assets are served straight from the integration through HA's brands proxy,
so no submission to [home-assistant/brands](https://github.com/home-assistant/brands)
is needed.

"M-Bus" and "Meter-Bus" refer to the EN 13757 standard; the M-Bus mark is the
property of the M-Bus Usergroup. This project is not affiliated with, endorsed
by or supported by the M-Bus Usergroup or any meter manufacturer.
