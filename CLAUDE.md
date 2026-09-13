# CLAUDE.md — Dometic CFX plugin

Repo-specific rules. Global rules in `~/.claude/CLAUDE.md` apply; end-user docs live in README.MD.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate.fish && pip install -r requirements.txt
black . && ruff check --select E9,F . && python -m pytest        # before every commit
scripts/sync_ddmp.sh ../dometic-ddmp vX.Y.Z                      # vendor the library at a tag
# deploy (Linux dev box)
/home/mike/programming/mike-local-development-scripts/deploy_indigo_plugin_to_server.sh \
    "Dometic CFX.indigoPlugin" /home/mike/programming/indigo-dometic-cfx
ssh mike@indigo.home.mikelamoureux.net 'bash -c "/usr/local/bin/indigo-restart-plugin com.vtmikel.dometiccfx"'
```

Verify deploys in `/Library/Application Support/Perceptive Automation/Indigo <version>/Logs/com.vtmikel.dometiccfx/plugin.log`,
not Events.txt. A brand-new bundle must be `open`ed on the Mac and "Install and Enable" clicked
once (the deploy script only updates files).

## Architecture

```
Dometic CFX.indigoPlugin/Contents/
  Info.plist                      # PluginVersion CalVer, id com.vtmikel.dometiccfx, ServerApiVersion 3.6
  Resources/icon.png              # rendered by scripts/make_icon.py (512x512); regenerate, never hand-edit
  Server Plugin/
    plugin.py                     # thin Indigo adapter: lifecycle, ConfigUIs, actions, menus, runConcurrentThread, _apply_outcome
    Devices.xml                   # one thermostat-type device "cfxCooler"; capability props are hidden ConfigUI fields
    Actions.xml / MenuItems.xml / PluginConfig.xml
    cfx/                          # Indigo-free (never `import indigo`)
      config.py                   #   CoolerConfig.from_props (never raises), validate_device_props, validate_prefs
      statemap.py                 #   CoolerState -> Indigo states, °C<->display unit, diff(); HVAC_POWER_TOPIC
      link.py                     #   CoolerLink state machine: connect / backoff / re-resolve by id / jobs / echo confirmation
    ddmp/                         # VENDORED copy of mike/dometic-ddmp (see VENDORED_DDMP_VERSION); never edit here
    VENDORED_DDMP_VERSION
tests/                            # conftest stubs `indigo`; FakeClient replays tests/fixtures/capture-2026-09-13.b64
scripts/sync_ddmp.sh · scripts/make_icon.py
```

## Invariants

- **The cooler is usually switched off.** That is a normal state: `connected=false`, last
  values kept, no red error state, retries capped at 60 s (`BACKOFF_SECONDS`). Only "not
  configured" and "wrong cooler at address" are error states.
- **Nothing blocks an Indigo callback.** Actions and menus only enqueue `Job`s; all network
  I/O happens in `runConcurrentThread` (sliced `self.sleep(0.5)`, never `Event.wait`) and the
  library's reader thread, which only enqueues events.
- **Indigo state follows the cooler, not the request.** A write is confirmed only when the
  cooler publishes the new value (it re-publishes the old value first, then the new one ~3 s
  later); only a NAK is a refusal. `ECHO_TIMEOUT` is 10 s.
- **Diff before update.** `_apply_outcome` pushes only changed states in one
  `updateStatesOnServer([...])` so triggers never fire on redundant writes.
- **Writes are default-deny in the library** (`csettemp`, `cpow`, `coolerpow`, `batprotlvl`,
  `icepow`). Never widen that list here; never touch the gateway/Wi-Fi/factory topics.
- **`HVAC_POWER_TOPIC`** (`cfx/statemap.py`) is the one place that decides what Cool/Off
  drives (`cpow`, matching the community BLE climate mapping). The master switch is the
  separate "Set Cooler Power" action.
- The library copy is vendored; fix bugs in `mike/dometic-ddmp`, tag, then re-run
  `scripts/sync_ddmp.sh`. `tests/test_vendored_ddmp.py` fails on drift.

## Production environment

- Cooler: Dometic CFX5 25 (`CFX525`, serial 52402647), MAC 14:33:5c:34:f1:2c, IoT VLAN
  10.66.40.129 (Kea reservation `dometic-cfx5`), firmware MC1_1.0.2. Indigo Mac is on the LAN
  VLAN; discovery crosses via the OPNsense mdns-repeater; TCP 13143 is allowed LAN→IoT.
- Verified live 2026-09-13: read path, set-point 1.0→2.0→1.0 °C, battery protection
  Medium→Low→Medium (all restored). Cooler power / compartment power writes are
  community-verified over Bluetooth with identical frames.
- Bookstack owner page for the network facts: "IoT LAN - Network & Services" (page 21).

## Release gating

- Gitea `mike/indigo-dometic-cfx` is the working remote (feature branch → PR → merge).
- `PluginVersion` is CalVer (`YYYY.N.N`); bump it in any PR that changes plugin behaviour.
- GitHub (`mlamoure/indigo-dometic-cfx`) is publish-on-public-release only, after Mike's
  explicit approval. `release_indigo_plugin_to_github.sh --gitea-only` for Gitea releases.
