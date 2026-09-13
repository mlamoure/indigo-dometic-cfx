# Dometic CFX — Indigo plugin

Controls **Dometic CFX5** portable coolers from [Indigo](https://www.indigodomo.com) over the
local Wi-Fi network. No cloud, no account, no Bluetooth: the plugin talks the cooler's own
LAN protocol (Dometic "DDMP v2") directly.

Dometic removed Wi-Fi control from its Mobile Cooling app in November 2025; the cooler
firmware still serves it. This plugin uses the [dometic-ddmp](https://git.home.mikelamoureux.net/mike/dometic-ddmp)
library (vendored into the bundle) to speak it.

## Features at a glance

| | |
|---|---|
| Device type | Native Indigo **thermostat**: cool set-point, Cool/Off mode, current temperature, cooling indicator (compressor). Works with control pages, Indigo Touch, scripts and the MCP Server's thermostat tools out of the box. |
| Extra states | Supply voltage, current draw, power source (AC/DC), battery protection level, door open, compressor running, cooler master power, ice maker, error codes/text, set-point range, product name, serial, firmware, last update. |
| Actions | Set Battery Protection (Low/Medium/High), Set Cooler Power (master on/off), Request Status. |
| Discovery | Finds coolers by mDNS (`_ddmp._tcp`) and Dometic's `DDMD` probe; re-finds a cooler by its id when its IP changes. |
| Off is normal | A switched-off cooler is expected: the device shows `connected = false`, keeps its last values, is **not** flagged as an error, and the plugin reconnects within a minute of power-on. |
| Live updates | The cooler pushes every change; there is no polling. |

## Requirements

- Indigo 2025.2 or later (Python 3.13). The bundle is self-contained; nothing is pip-installed.
- A Dometic CFX5 that is already joined to your Wi-Fi (firmware MC1 1.x; tested on a CFX5 25 with 1.0.2). Wi-Fi provisioning itself is not part of this plugin.
- TCP port 13143 reachable from the Indigo Mac to the cooler. If they are on different VLANs, discovery needs an mDNS repeater (or enter the address manually); a DHCP reservation for the cooler is recommended.

## Installation

1. Download the latest release zip and double-click the `.indigoPlugin` bundle (or drop it in Indigo's Plugins folder), then choose **Install and Enable**.
2. **Plugins → Dometic CFX → Configure**: set the connect timeout and log level (defaults are fine).
3. **Devices → New → Dometic CFX → Dometic CFX5 Cooler**: click **Discover Coolers**, pick the cooler (or choose manual entry and type its address), pick the display unit, save.

The device starts populating within a few seconds while the cooler is on.

## Plugin configuration

| Setting | Default | Meaning |
|---|---|---|
| Connect timeout | 5 s | How long a connection attempt may take before the cooler is considered off (2–30). |
| Event logging level | Informational | Standard Indigo log levels; the plugin's own file log always keeps debug detail. |

## Device configuration

| Field | Meaning |
|---|---|
| Cooler | A cooler found by **Discover Coolers**, or "Enter the address manually". The cooler's id (its MAC) is stored so the plugin can re-find it after an IP change. |
| Address / Port | IP or hostname and TCP port (13143). Filled in from discovery. |
| Temperature unit | Fahrenheit or Celsius for the Indigo states (the protocol is always Celsius). |
| Compartment | First (single zone) or second (dual zone: create one device per compartment). |

## Devices & states

Built-in thermostat states: `temperatureInput1` (measured), `setpointCool`, `hvacOperationMode`
(Cool = compartment power on, Off = off), `hvacCoolerIsOn` (compressor running).

Custom states: `connected`, `coolerPowerOn`, `compartmentPowerOn`, `compressorOn`, `doorOpen`,
`voltage` (V), `current` (A), `powerSource` (`AC`/`DC`), `batteryProtection` (`Low`/`Medium`/`High`),
`iceMakerOn`, `errorCodes` (e.g. `23,16`), `errorText`, `hasError`, `setpointMin`, `setpointMax`,
`productName`, `serialNumber`, `firmwareVersion`, `lastUpdate`.

Set-point changes from the Indigo UI, scripts or actions are sent to the cooler and the Indigo
state changes only once the cooler confirms the new value (about 3 s). Set-points are clamped
to the cooler's own range (−22 to 20 °C on a CFX5 25) and the cooler stores them with 0.1 °C
granularity.

## Actions

- **Set Battery Protection** — Low, Medium or High (the cooler's DC cut-off level).
- **Set Cooler Power** — the master power switch (`coolerpow`). The thermostat's Cool/Off mode
  drives compartment power (`cpow`) instead, which is what the display's zone button does.
- **Request Status** — re-subscribes to every value.

## Menu items

- **Discover Coolers** — browse the network and log what answers (only powered-on coolers reply).
- **Toggle Debug Logging**

## Triggers

Useful state changes to trigger on: `doorOpen` (left open), `hasError` / `errorText`,
`powerSource` (mains lost), `voltage` (battery sagging), `connected` (cooler switched on/off),
`compressorOn`.

## Troubleshooting

- **Device shows `connected = false` and nothing updates** — the cooler is switched off or
  asleep, or not reachable on port 13143. The plugin keeps retrying every minute; check
  `.../Logs/com.vtmikel.dometiccfx/plugin.log`.
- **"wrong cooler at address"** — the address answered with a different cooler id; the plugin
  will re-discover the configured cooler by id. Fix the address or clear the id in the device
  settings.
- **Discovery finds nothing** — only powered-on coolers answer; across VLANs mDNS needs a
  repeater. Enter the address manually; the plugin probes it and learns the cooler id.
- **A set-point change is logged as "not confirmed"** — the cooler did not publish the new
  value within 10 s (it was switched off mid-write, or refused an out-of-range value).

## Security note

The cooler's Wi-Fi channel has no authentication: anyone who can reach port 13143 can change
the set-point or switch the cooler off. Keep the cooler on an isolated (IoT) network and only
allow the Indigo host to reach it.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate.fish && pip install -r requirements.txt
black . && ruff check --select E9,F . && python -m pytest
scripts/sync_ddmp.sh ../dometic-ddmp v0.1.1     # refresh the vendored library
```
