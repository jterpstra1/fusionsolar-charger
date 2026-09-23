"""Home Assistant add-on entrypoint.

- Polls the charger and pushes live sensors to HA core.
- Keeps an hourly lifetime-energy series in /data/hourly.json, backfilled once from
  FusionSolar history, and mirrors it into the HA long-term statistic
  fusionsolar:<prefix>_energy (sum = the charger's lifetime kWh counter).
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
PREFIX = "scharger"  # entity prefix, set from the add-on options in main()


def stat_meta() -> dict:
    return {
        "statistic_id": f"fusionsolar:{PREFIX}_energy", "source": "fusionsolar",
        "name": f"{PREFIX} energy charged", "unit_of_measurement": "kWh", "unit_class": "energy",
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


PUBLISHED = {}  # entity_id -> last payload, re-sent when HA restarts and forgets the states


def publish(entity_id, state, **attrs):
    payload = {"state": state, "attributes": attrs}
    r = requests.post(f"{CORE}/states/{entity_id}", headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    PUBLISHED[entity_id] = payload


def wait_and_watch(seconds: int, check_every: int = 15):
    """Sleep until the next poll; meanwhile re-publish our states as soon as HA core has
    restarted (states set through the API don't survive a restart)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(check_every, max(deadline - time.monotonic(), 0)))
        if not PUBLISHED:
            continue
        probe = next(iter(PUBLISHED))
        try:
            r = requests.get(f"{CORE}/states/{probe}", headers=HEADERS, timeout=10)
            if r.status_code == 404:
                for entity_id, payload in list(PUBLISHED.items()):
                    requests.post(f"{CORE}/states/{entity_id}", headers=HEADERS,
                                  json=payload, timeout=15).raise_for_status()
                print("Home Assistant restarted; re-published last known states.")
        except requests.RequestException:
            pass  # core is still starting; try again next round


def ha_timezone() -> ZoneInfo:
    r = requests.get(f"{CORE}/config", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return ZoneInfo(r.json()["time_zone"])


def row_start(ts: int) -> int:
    """HA statistic rows cover [start, start+1h) and hold the value at the end of that hour,
    so a reading taken exactly on the hour closes the previous row."""
    return (ts - 1) // 3600 * 3600


STATS_VERSION = 2  # bump to force a full re-import when the row format changes


def import_statistics(hourly: dict, only_from: int = 0):
    """Write hourly rows (start epoch -> kWh) >= only_from to HA's long-term statistics.

    state is the lifetime counter; sum counts from the first reading, so the energy charged
    before the history starts doesn't land in the first month."""
    rows, last, base = [], 0.0, None
    for start in sorted(int(k) for k in hourly):
        value = max(hourly[str(start)], last)  # lifetime counter never decreases
        last = value
        base = value if base is None else base
        if start >= only_from:
            rows.append({"start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                         "state": value, "sum": round(value - base, 3)})
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
                                "metadata": stat_meta(), "stats": rows[i:i + 1000]}))
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
    if state.get("stats_version") == STATS_VERSION:  # otherwise main() imports everything
        first = datetime(first_day.year, first_day.month, first_day.day, tzinfo=tz)
        import_statistics(hourly, only_from=int(first.timestamp()) - 3600)


def find_history_start(reader, tz: ZoneInfo, years_back: int = 6) -> date:
    """Probe the 1st of each month (oldest first) for history; data may begin in the
    month before the first hit, so start there."""
    today = datetime.now(tz).date()
    month = date(today.year - years_back, today.month, 1)
    while month <= today:
        noon = datetime(month.year, month.month, 1, 12, tzinfo=tz)
        if reader.energy_history(int(noon.timestamp() * 1000)):
            return (month - timedelta(days=1)).replace(day=1)
        month = (month + timedelta(days=32)).replace(day=1)
        time.sleep(0.3)
    return today.replace(day=1)


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
    common = dict(unit_of_measurement="kWh", device_class="energy", icon="mdi:ev-station")
    if c_this is not None:
        publish(f"sensor.{PREFIX}_energy_this_month", round(total_now - c_this, 3),
                friendly_name="Energy charged this month", period=this_start.strftime("%Y-%m"),
                meter_start_kwh=c_this, meter_now_kwh=total_now, **common)
    if c_this is not None and c_last is not None:
        publish(f"sensor.{PREFIX}_energy_last_month", round(c_this - c_last, 3),
                friendly_name="Energy charged last month", period=last_start.strftime("%Y-%m"),
                meter_start_kwh=c_last, meter_end_kwh=c_this, **common)


def push_live(snap):
    if snap["total_energy_kwh"] is not None:
        publish(f"sensor.{PREFIX}_total_energy", snap["total_energy_kwh"],
                friendly_name="Charger total energy", unit_of_measurement="kWh",
                device_class="energy", state_class="total_increasing", icon="mdi:ev-station")
    if snap["power_kw"] is not None:
        publish(f"sensor.{PREFIX}_power", snap["power_kw"],
                friendly_name="Charging power", unit_of_measurement="kW",
                device_class="power", state_class="measurement", icon="mdi:ev-plug-type2")
    publish(f"sensor.{PREFIX}_status", snap["working_status"] or "unknown",
            friendly_name="Charger status", icon="mdi:ev-station",
            current_a=snap["current_a"], grid_voltage_v=snap["grid_voltage_v"],
            lock_status=snap["lock_status"], relay=snap["relay"],
            temperature_c=snap["temperature_c"], wifi_dbm=snap["wifi_dbm"], updated=snap["time"])


def main():
    global PREFIX
    opts = json.load(open("/data/options.json"))
    if not opts.get("username") or not opts.get("password"):
        raise SystemExit("Set username and password in the add-on configuration.")
    PREFIX = (opts.get("entity_prefix") or "scharger").strip().lower()
    interval = max(int(opts.get("interval", 300)), 60)
    tz = ha_timezone()
    hourly, state = load(STORE, {}), load(STATE, {})
    reader = None
    while True:
        try:
            if reader is None:
                reader = ChargerReader(opts["username"], opts["password"],
                                       opts.get("subdomain") or "region01eu5", opts.get("charger_sn") or "")
                print(f"Logged in; charger {reader.charger_sn} (dnId {reader.charger_id}, "
                      f"connector {reader.connector_id})")
                if opts.get("backfill", True):
                    if "backfill_start" not in state:
                        start = opts.get("backfill_from") or find_history_start(reader, tz).isoformat()
                        state["backfill_start"] = str(start)
                        save(STATE, state)
                    backfill(reader, hourly, state, date.fromisoformat(state["backfill_start"]), tz)
            if hourly and state.get("stats_version") != STATS_VERSION:
                import_statistics(hourly)
                state["stats_version"] = STATS_VERSION
                save(STATE, state)
                print(f"Imported {len(hourly)} hourly readings into HA statistics.")
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
        wait_and_watch(interval)


if __name__ == "__main__":
    main()
