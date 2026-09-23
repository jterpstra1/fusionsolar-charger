# FusionSolar Charger (Home Assistant add-on)

Reads a Huawei **SCharger** (e.g. SCharger-22KT-S0) from the FusionSolar web portal and
publishes it to Home Assistant. Built to get a reliable **monthly kWh figure for a company
car**, because the FusionSolar reports are hard to use for that.

The SCharger has no readable local interface (its Modbus port acts as a *master* that asks
an inverter for meter data), so this add-on uses the portal instead: the same internal
endpoints the charger page in the FusionSolar website calls. It is **not an official API**
and may break when Huawei changes the portal.

## What you get

| Entity | Meaning |
|---|---|
| `sensor.company_car_kwh_this_month` | kWh charged since the 1st of this month |
| `sensor.company_car_kwh_last_month` | kWh charged last month (attributes: period, meter start/end) |
| `sensor.scharger_total_energy` | charger's lifetime energy counter (kWh) |
| `sensor.scharger_power` | charging power (kW) |
| `sensor.scharger_status` | working status; current/voltage per phase, lock, relay, temperature, WiFi as attributes |
| statistic `fusionsolar:scharger_energy` | hourly long-term statistic of the lifetime counter, backfilled from FusionSolar history |

Chart per month (for a screenshot):

```yaml
type: statistics-graph
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

1. Logs in to FusionSolar with [`fusion_solar_py`](https://github.com/EnergieID/FusionSolar),
   finds the charger (`mocId 60080`) and its charging connector (`mocId 60081`) on the account.
2. On first start, backfills the connector's lifetime-energy history (signal `30003`, hourly)
   day by day from `backfill_from`, and imports it as the statistic above. Progress is kept in
   `/data`, so a restart resumes where it stopped.
3. Every `interval` seconds it reads the live values, pushes the sensors through the
   Supervisor API and updates the current hour in the statistic.

FusionSolar itself only refreshes charger data about every 5 minutes.

## Install

1. In FusionSolar, create a separate user for the add-on (**Plant → Plant Users → Add User**),
   so the add-on's logins don't end your own sessions.
2. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add this
   repository's URL, then install **FusionSolar Charger**.
3. Configure:

   | Option | Default | |
   |---|---|---|
   | `username` / `password` | | the FusionSolar account from step 1 |
   | `subdomain` | `uni002eu5` | first part of the portal URL after logging in |
   | `interval` | `300` | poll interval in seconds (60–3600) |
   | `backfill_from` | `2023-12-01` | first day to import history for |

4. Start it and enable **Start on boot** and **Watchdog**.

## Command line

`fusionsolar_charger/charger.py` also runs on its own (Python 3.10+, `pip install fusion_solar_py`):

```bash
FUSIONSOLAR_USER=you@example.com FUSIONSOLAR_PASSWORD=... python fusionsolar_charger/charger.py --loop 300 --out charger.jsonl
```

On macOS the password can come from the keychain instead
(`security add-generic-password -s fusionsolar -a you@example.com -w`).
