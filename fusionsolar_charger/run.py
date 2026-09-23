"""Home Assistant add-on entrypoint.

- Polls the charger and pushes live sensors to HA core.
- Keeps an hourly lifetime-energy series in /data/hourly.json, backfilled once from
  FusionSolar history, and mirrors it into the HA long-term statistic
  fusionsolar:scharger_energy (sum = charger's lifetime kWh counter).
- Publishes kWh charged this month and last month, computed from that series.
"""
import json
import os
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import websocket

from charger import ChargerReader

CORE = "http://supervisor/core/api"
TOKEN = os.environ["SUPERVISOR_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
STORE = "/data/hourly.json"
STATE = "/data/state.json"
STAT_ID = "fusionsolar:scharger_energy"
STAT_META = {
    "statistic_id": STAT_ID, "source": "fusionsolar", "name": "SCharger energy charged",
    "unit_of_measurement": "kWh", "unit_class": "energy",
    "has_mean": False, "mean_type": 0, "has_sum": True,
}


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, data):
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def publish(entity_id, state, **attrs):
    r = requests.post(f"{CORE}/states/{entity_id}", headers=HEADERS,
                      json={"state": state, "attributes": attrs}, timeout=15)
    r.raise_for_status()


def ha_timezone() -> ZoneInfo:
    r = requests.get(f"{CORE}/config", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return ZoneInfo(r.json()["time_zone"])


def row_start(ts: int) -> int:
    """HA statistic rows cover [start, start+1h) and hold the value at the end of that hour,
    so a reading taken exactly on the hour closes the previous row."""
    return (ts - 1) // 3600 * 3600


def import_statistics(hourly: dict, only_from: int = 0):
    """Write hourly rows (start epoch -> kWh) >= only_from to HA's long-term statistics."""
    rows, last = [], 0.0
    for start in sorted(int(k) for k in hourly):
        value = max(hourly[str(start)], last)  # lifetime counter never decreases
        last = value
        if start >= only_from:
            rows.append({"start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                         "state": value, "sum": value})
    if not rows:
        return
    ws = websocket.create_connection("ws://supervisor/core/websocket", timeout=30)
    try:
        ws.recv()
        ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        if json.loads(ws.recv()).get("type") != "auth_ok":
            raise RuntimeError("HA websocket auth failed")
        for i in range(0, len(rows), 1000):
            ws.send(json.dumps({"id": i + 1, "type": "recorder/import_statistics",
                                "metadata": STAT_META, "stats": rows[i:i + 1000]}))
            while True:
                r = json.loads(ws.recv())
                if r.get("id") == i + 1:
                    break
            if not r.get("success"):
                raise RuntimeError(f"import_statistics failed: {r.get('error')}")
    finally:
        ws.close()


def backfill(reader, hourly: dict, state: dict, start_day: date, tz: ZoneInfo):
    day = date.fromisoformat(state.get("backfilled_through", "")) + timedelta(days=1) \
        if state.get("backfilled_through") else start_day
    today = datetime.now(tz).date()
    if day > today:
        return
    first_day = day
    print(f"Backfilling FusionSolar history from {day} to {today}...")
    while day <= today:
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=tz)
        for ts, value in reader.energy_history(int(noon.timestamp() * 1000)):
            key = str(row_start(ts))
            hourly[key] = max(hourly.get(key, 0.0), value)
        if day < today:
            state["backfilled_through"] = day.isoformat()
        if day.day == 1 or day == today:
            save(STORE, hourly)
            save(STATE, state)
            print(f"  ...{day} ({len(hourly)} hourly readings)")
        day += timedelta(days=1)
        time.sleep(0.3)
    if state.get("imported"):
        first = datetime(first_day.year, first_day.month, first_day.day, tzinfo=tz)
        import_statistics(hourly, only_from=int(first.timestamp()) - 3600)
    else:
        import_statistics(hourly)
        state["imported"] = True
        save(STATE, state)
        print(f"Backfill imported into HA statistics ({len(hourly)} hourly readings).")


def counter_at(hourly: dict, ts: int):
    """Lifetime kWh at time ts: the row that ends at or before ts."""
    best = None
    for k, v in hourly.items():
        end = int(k) + 3600
        if end <= ts and (best is None or end > best[0]):
            best = (end, v)
    return best[1] if best else None


def month_sensors(hourly: dict, total_now: float, tz: ZoneInfo):
    now = datetime.now(tz)
    this_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_start = (this_start - timedelta(days=1)).replace(day=1)
    c_this = counter_at(hourly, int(this_start.timestamp()))
    c_last = counter_at(hourly, int(last_start.timestamp()))
    common = dict(unit_of_measurement="kWh", device_class="energy", icon="mdi:car-electric")
    if c_this is not None:
        publish("sensor.company_car_kwh_this_month", round(total_now - c_this, 3),
                friendly_name="Company car kWh this month", period=this_start.strftime("%Y-%m"),
                meter_start_kwh=c_this, meter_now_kwh=total_now, **common)
    if c_this is not None and c_last is not None:
        publish("sensor.company_car_kwh_last_month", round(c_this - c_last, 3),
                friendly_name="Company car kWh last month", period=last_start.strftime("%Y-%m"),
                meter_start_kwh=c_last, meter_end_kwh=c_this, **common)


def push_live(snap):
    if snap["total_energy_kwh"] is not None:
        publish("sensor.scharger_total_energy", snap["total_energy_kwh"],
                friendly_name="SCharger total energy charged", unit_of_measurement="kWh",
                device_class="energy", state_class="total_increasing", icon="mdi:ev-station")
    if snap["power_kw"] is not None:
        publish("sensor.scharger_power", snap["power_kw"],
                friendly_name="SCharger charging power", unit_of_measurement="kW",
                device_class="power", state_class="measurement", icon="mdi:ev-plug-type2")
    publish("sensor.scharger_status", snap["working_status"] or "unknown",
            friendly_name="SCharger status", icon="mdi:ev-station",
            current_a=snap["current_a"], grid_voltage_v=snap["grid_voltage_v"],
            lock_status=snap["lock_status"], relay=snap["relay"],
            temperature_c=snap["temperature_c"], wifi_dbm=snap["wifi_dbm"], updated=snap["time"])


def main():
    opts = json.load(open("/data/options.json"))
    if not opts.get("username") or not opts.get("password"):
        raise SystemExit("Set username and password in the add-on configuration.")
    interval = max(int(opts.get("interval", 300)), 60)
    start_day = date.fromisoformat(opts.get("backfill_from") or "2023-12-01")
    tz = ha_timezone()
    hourly, state = load(STORE, {}), load(STATE, {})
    reader = None
    while True:
        try:
            if reader is None:
                reader = ChargerReader(opts["username"], opts["password"], opts.get("subdomain", "uni002eu5"))
                print(f"Logged in; charger dnId={reader.charger_id} connector dnId={reader.connector_id}")
                backfill(reader, hourly, state, start_day, tz)
            snap = reader.snapshot()
            push_live(snap)
            total = snap["total_energy_kwh"]
            if total is not None:
                start = row_start(int(time.time()))
                hourly[str(start)] = max(hourly.get(str(start), 0.0), total)
                save(STORE, hourly)
                import_statistics(hourly, only_from=start - 3600)
                month_sensors(hourly, total, tz)
            print(json.dumps(snap))
        except Exception:
            traceback.print_exc()
            reader = None  # force a fresh login next round
        time.sleep(interval)


if __name__ == "__main__":
    main()
