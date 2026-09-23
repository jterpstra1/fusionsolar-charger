#!/usr/bin/env python3
"""Read Huawei SCharger data from the FusionSolar web portal (owner login).

Uses the same internal endpoints as the portal's charger page. Not an official
API, so it may break when Huawei changes the portal.

Credentials:
  FUSIONSOLAR_USER      login email/username
  FUSIONSOLAR_PASSWORD  password (or store it in the macOS keychain:
                        security add-generic-password -s fusionsolar -a <user> -w)
  FUSIONSOLAR_SUBDOMAIN portal subdomain, e.g. uni002eu5 (see the URL after logging in)
  FUSIONSOLAR_CHARGER   optional charger serial number, if the account has several

Usage:
  charger.py                      print one JSON snapshot
  charger.py --loop 300 --out charger.jsonl   append a snapshot every 5 min
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from fusion_solar_py.client import FusionSolarClient

MOC_CHARGER = 60080
MOC_CONNECTOR = 60081


def get_password(user: str) -> str:
    pw = os.environ.get("FUSIONSOLAR_PASSWORD")
    if pw:
        return pw
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", "fusionsolar", "-a", user, "-w"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("No password: set FUSIONSOLAR_PASSWORD or add it to the keychain (service 'fusionsolar').")


class ChargerReader:
    def __init__(self, user: str, password: str, subdomain: str, charger_sn: str = ""):
        self.client = FusionSolarClient(user, password, huawei_subdomain=subdomain)
        self.base = f"https://{subdomain}.fusionsolar.huawei.com"
        self.charger_sn = charger_sn.strip().upper()
        self.charger_id, self.connector_id = self._discover()

    def _post(self, path: str, body) -> dict:
        # keep_alive() re-logs in if needed and refreshes the roarand (CSRF) header
        self.client.keep_alive()
        r = self.client._session.post(self.base + path, json=body)
        r.raise_for_status()
        return r.json()

    def _tree(self, parent_dn: str, moc_ids=None) -> list:
        filter_cond = {"nameType": "device"}
        if moc_ids:
            filter_cond["mocIdInclude"] = moc_ids
        data = self._post("/rest/dp/pvms/organization/v1/tree", {
            "parentDn": parent_dn, "treeDepth": "device",
            "pageParam": {"needPage": True},
            "filterCond": filter_cond,
            "displayCond": {"self": False, "status": True},
        })
        nodes, stack = [], list(data.get("childList", []))
        while stack:
            n = stack.pop()
            nodes.append(n)
            stack.extend(n.get("childList") or [])
        return nodes

    def _discover(self):
        """Find the charger (first one, or the one whose name/SN matches charger_sn) and its connector."""
        found = []
        for plant_dn in self.client.get_plant_ids():
            chargers = [n for n in self._tree(plant_dn) if n.get("mocId") == MOC_CHARGER]
            found += [n.get("nodeName") for n in chargers]
            if self.charger_sn:
                chargers = [n for n in chargers if self.charger_sn in (n.get("nodeName") or "").upper()]
            for charger in chargers:
                self.charger_sn = self.charger_sn or charger.get("nodeName", "")
                children = self._tree(charger["elementDn"], [MOC_CONNECTOR])
                connector = next((n for n in children if n.get("mocId") == MOC_CONNECTOR), None)
                if connector is None:
                    raise RuntimeError(f"No charging connector under {charger['elementDn']}: "
                                       f"{[(n.get('nodeName'), n.get('mocId')) for n in children]}")
                return int(charger["elementId"]), int(connector["elementId"])
        raise SystemExit(f"Charger {self.charger_sn!r} not found; chargers on this account: {found}"
                         if self.charger_sn else "No charger found on this account.")

    def energy_history(self, day_ms: int) -> list:
        """Lifetime-energy readings (epoch s, kWh) for the day containing day_ms.

        The portal returns a 5-minute grid; empty slots hold float max as a placeholder.
        """
        data = self._post("/rest/neteco/web/homemgr/v1/device/history/device-history-data",
                          [{"dnId": str(self.connector_id), "signalIds": [30003], "date": day_ms}])
        points = data.get(str(self.connector_id), {}).get("30003", {}).get("pmDataList", [])
        return [(p["startTime"], p["counterValue"]) for p in points
                if p.get("counterValue") is not None and p["counterValue"] < 1e12]

    def snapshot(self) -> dict:
        data = self._post("/rest/neteco/web/homemgr/v1/device/get-realtime-info", {"conditions": [
            {"dnId": self.charger_id, "queryAll": True},
            {"dnId": self.connector_id, "queryAll": True},
        ]})
        status = self._post("/rest/neteco/web/homemgr/v1/charger/status/charge-status", {"dnId": self.charger_id})

        def signals(dn_id):
            return {s["id"]: s for s in data.get(str(dn_id), [])}

        def num(sig):
            try:
                return float(sig["value"])
            except (KeyError, TypeError, ValueError):
                return None

        ch, co = signals(self.charger_id), signals(self.connector_id)
        return {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "charge_status": status.get("chargeStatus"),
            "working_status": co.get(10004, {}).get("realValue"),
            "power_kw": num(co.get(10013, {})),
            "current_a": [num(co.get(i, {})) for i in (10032, 10034, 10033)],  # L1, L2, L3
            "grid_voltage_v": [num(ch.get(i, {})) for i in (2101259, 2101260, 2101261)],
            "total_energy_kwh": num(co.get(10014, {})),
            "lock_status": co.get(10023, {}).get("realValue"),
            "relay": co.get(2101254, {}).get("realValue"),
            "temperature_c": num(ch.get(2101271, {})),
            "wifi_dbm": num(ch.get(15101, {})),
        }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="poll every N seconds (min 60)")
    ap.add_argument("--out", help="append JSON lines to this file instead of stdout")
    args = ap.parse_args()

    user = os.environ.get("FUSIONSOLAR_USER") or sys.exit("Set FUSIONSOLAR_USER.")
    reader = ChargerReader(user, get_password(user), os.environ.get("FUSIONSOLAR_SUBDOMAIN", "region01eu5"),
                           os.environ.get("FUSIONSOLAR_CHARGER", ""))

    while True:
        line = json.dumps(reader.snapshot())
        if args.out:
            with open(args.out, "a") as f:
                f.write(line + "\n")
        print(line, flush=True)
        if not args.loop:
            break
        time.sleep(max(args.loop, 60))


if __name__ == "__main__":
    main()
