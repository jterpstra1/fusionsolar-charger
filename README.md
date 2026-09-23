# FusionSolar Charger (Home Assistant add-on)

Brings a Huawei **SCharger** (e.g. SCharger-22KT-S0) into Home Assistant via the FusionSolar
web portal: live status and power, the lifetime energy counter, **kWh charged this month and
last month**, and the charger's full history as a long-term statistic. Handy when you need a
monthly figure, e.g. to get reimbursed for charging a company car at home.

The SCharger has no readable local interface (its Modbus port acts as a *master* that asks an
inverter for meter data), so this add-on uses the portal instead: the same internal endpoints
the charger page of the FusionSolar website calls. It is **not an official API** and may break
when Huawei changes the portal.

## Install

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fjterpstra1%2Ffusionsolar-charger)

1. In FusionSolar, create a separate user for the add-on (**Plant → Plant Users → Add User**)
   and log in with it once in a browser. A dedicated user keeps the add-on's logins from ending
   your own sessions.
2. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/jterpstra1/fusionsolar-charger` (or use the button above), then install
   **FusionSolar Charger**.
3. Set at least `username`, `password` and `subdomain` (see below), start it, and enable
   **Start on boot** and **Watchdog**.

## Options

| Option | Default | Description |
|---|---|---|
| `username` / `password` | | FusionSolar login (the user from step 1) |
| `subdomain` | `region01eu5` | First part of the portal address after you log in, e.g. `uni002eu5` for `https://uni002eu5.fusionsolar.huawei.com/...` |
| `charger_sn` | *(first charger)* | Serial number or device name of the charger, if the account has several |
| `entity_prefix` | `scharger` | Prefix for all entity ids and the statistic id |
| `interval` | `300` | Poll interval in seconds (60–3600). FusionSolar refreshes charger data about every 5 minutes |
| `backfill` | `true` | Import the charger's history on first start |
| `backfill_from` | *(auto)* | First day to import (`YYYY-MM-DD`). Empty: detect where the history starts |

## What you get

With the default prefix `scharger`:

| Entity | Meaning |
|---|---|
| `sensor.scharger_energy_this_month` | kWh charged since the 1st of this month |
| `sensor.scharger_energy_last_month` | kWh charged last month (attributes: `period`, meter reading at start and end) |
| `sensor.scharger_total_energy` | lifetime energy counter (kWh) |
| `sensor.scharger_power` | charging power (kW) |
| `sensor.scharger_status` | working status; current and voltage per phase, lock, relay, temperature and WiFi signal as attributes |
| statistic `fusionsolar:scharger_energy` | hourly long-term statistic of the lifetime counter, including the backfilled history |

These entities are created through the Supervisor API, so they have no unique id and can't be
edited in the UI. They come back within one poll after a Home Assistant restart.

### Dashboard example

```yaml
type: vertical-stack
cards:
  - type: tile
    entity: sensor.scharger_energy_last_month
    name: Last month
  - type: tile
    entity: sensor.scharger_energy_this_month
    name: This month
  - type: statistics-graph
    title: kWh charged per month
    entities:
      - entity: fusionsolar:scharger_energy
        name: Charged
    stat_types: [change]
    period: month
    chart_type: bar
    days_to_show: 365
```

## How it works

1. Logs in with [`fusion_solar_py`](https://github.com/EnergieID/FusionSolar) and looks up the
   charger (`mocId 60080`) and its charging connector (`mocId 60081`) on the account.
2. On first start it finds where the charger's history begins, then imports the lifetime-energy
   history (signal `30003`, hourly) day by day. Progress is stored in the add-on's `/data`, so a
   restart resumes where it stopped. Expect a few minutes per year of history.
3. Every `interval` seconds it reads the live values, pushes the sensors and updates the current
   hour of the statistic. The monthly sensors are computed from the counter at the month
   boundaries, so they are right for months before the add-on was installed too.

## Command line

`fusionsolar_charger/charger.py` also runs on its own (Python 3.10+, `pip install fusion_solar_py`):

```bash
FUSIONSOLAR_USER=you@example.com FUSIONSOLAR_PASSWORD=... FUSIONSOLAR_SUBDOMAIN=uni002eu5 \
  python fusionsolar_charger/charger.py --loop 300 --out charger.jsonl
```

On macOS the password can come from the keychain instead
(`security add-generic-password -s fusionsolar -a you@example.com -w`).
