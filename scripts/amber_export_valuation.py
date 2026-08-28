#!/usr/bin/env python3
"""Valuation pipeline — marry Amber feed-in export kWh with Amber wholesale spot price.

For every day in range (back to the 90-day API reach):
  - export_value_dollars = sum over 5-min feedIn intervals of (kwh * spotPerKwh / 100)
      (billing-grade; spot can go negative -> you PAY to export)
  - feedin_kwh = Amber billing-recorded export
  - Merges GoodWe daily snapshot (load_kwh, solar_kwh, goodwe_export_kwh) for context.
  - Computes the refined "assumed savings" figure:
      hypothetical = load_kwh * blended_import_rate          (what all energy used would cost)
      actual_net   = import_cost - export_value              (what you actually paid, net of export)
      assumed_savings = hypothetical - actual_net

Output: data/export-valuation.json  (used by energy.html Calendar tab)
"""
import json, ssl, urllib.request, os, re, sys
from datetime import date, timedelta, datetime, timezone
from collections import defaultdict

GARY_PAGES = os.path.expanduser("~/Projects/gary-pages")
DATA_FILE = os.path.join(GARY_PAGES, "data", "export-valuation.json")

PRICE_SCRIPT = os.path.expanduser("~/.claude/scripts/amber_price_alert.py")
_creds = {}
with open(PRICE_SCRIPT) as f:
    content = f.read()
    for key in ["AMBER_TOKEN", "SITE_ID"]:
        m = re.search(rf'{key}\s*=\s*"([^"]+)"', content)
        if m:
            _creds[key] = m.group(1)
AMBER_TOKEN = _creds.get("AMBER_TOKEN", "")
SITE_ID = _creds.get("SITE_ID", "")

ADELAIDE = timezone(timedelta(hours=9, minutes=30))


def get_ssl_ctx():
    ctx = ssl.create_default_context()
    for p in ["/usr/local/etc/openssl@3/cert.pem", "/usr/local/etc/openssl/cert.pem", "/etc/ssl/cert.pem"]:
        if os.path.exists(p):
            ctx.load_verify_locations(p)
            break
    return ctx


def fetch_usage(start_str, end_str):
    url = f"https://api.amber.com.au/v1/sites/{SITE_ID}/usage?startDate={start_str}&endDate={end_str}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {AMBER_TOKEN}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=get_ssl_ctx()) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  fetch err {start_str}..{end_str}: {e}", file=sys.stderr)
        return []


def load_goodwe_history():
    p = os.path.join(GARY_PAGES, "data", "energy-history.json")
    try:
        with open(p) as f:
            return {e["date"]: e for e in json.load(f)}
    except Exception:
        return {}


def load_amber_daily():
    p = os.path.join(GARY_PAGES, "data", "amber-daily.json")
    try:
        with open(p) as f:
            return {e["date"]: e for e in json.load(f)}
    except Exception:
        return {}


def main():
    end = date.today()
    # reach back to earliest available (90 days); start at May 27 to match existing data
    start = end - timedelta(days=92)
    if start < date(2026, 5, 27):
        start = date(2026, 5, 27)

    print(f"Fetching {start} .. {end} (7-day chunks)...")
    days = defaultdict(lambda: {
        "feedin_kwh": 0.0,
        "feedin_value_cents": 0.0,
        "spot_weighted_cents": 0.0,   # sum of spot*kwh for avg calc
        "n_feedin": 0,
        "import_kwh": 0.0,
        "import_cost_cents": 0.0,
    })

    chunk = start
    while chunk <= end:
        ce = min(chunk + timedelta(days=6), end)
        data = fetch_usage(chunk.isoformat(), ce.isoformat())
        for it in data:
            d = it.get("date", "")
            if not d:
                continue
            ch = it.get("channelType", "")
            kwh = it.get("kwh", 0) or 0
            spot = it.get("spotPerKwh", 0) or 0
            cost_c = it.get("cost", 0) or 0
            if ch == "feedIn":
                # kWh positive = exported. Spot = wholesale feed-in rate (cents/kWh).
                days[d]["feedin_kwh"] += kwh
                days[d]["feedin_value_cents"] += kwh * spot
                days[d]["spot_weighted_cents"] += kwh * spot  # same as value cents weighted
                days[d]["n_feedin"] += 1
            elif ch == "general" and it.get("channelIdentifier") == "E1":
                days[d]["import_kwh"] += kwh
                days[d]["import_cost_cents"] += cost_c
        chunk = ce + timedelta(days=1)

    gw = load_goodwe_history()
    amb = load_amber_daily()

    result = []
    for d in sorted(days):
        info = days[d]
        feedin_kwh = round(info["feedin_kwh"], 3)
        export_value = round(info["feedin_value_cents"] / 100.0, 3)
        avg_spot = round(info["spot_weighted_cents"] / info["feedin_kwh"], 2) if info["feedin_kwh"] else None
        import_cost = round(info["import_cost_cents"] / 100.0, 3)
        import_kwh = round(info["import_kwh"], 3)

        g = gw.get(d, {})
        load_kwh = g.get("load_kwh")
        solar_kwh = g.get("solar_kwh")
        goodwe_export = g.get("grid_export_kwh")
        battery_discharge = g.get("battery_discharge_kwh")

        # Blended import rate from Amber billing (what they actually paid for imported kWh)
        blended_rate_c = (info["import_cost_cents"] / info["import_kwh"]) if info["import_kwh"] > 0 else None

        # Refined assumed-savings: hypothetical all-grid cost vs actual net outlay
        assumed_savings = None
        if load_kwh is not None and blended_rate_c is not None:
            hypothetical = load_kwh * blended_rate_c / 100.0
            actual_net = import_cost - export_value
            assumed_savings = round(hypothetical - actual_net, 3)
        elif load_kwh is not None and import_kwh <= 0.001 and import_cost <= 0.001:
            # zero import all day -> actual net is just (-export_value)
            hypothetical = 0.0 if blended_rate_c is None else load_kwh * blended_rate_c / 100.0
            # fall back: assume blended = amber avg if no import
            actual_net = -export_value
            if hypothetical:
                assumed_savings = round(hypothetical - actual_net, 3)
            else:
                assumed_savings = round(export_value, 3)  # saved = what export earned + avoided

        row = {
            "date": d,
            "feedin_kwh": feedin_kwh,
            "export_value_dollars": export_value,
            "avg_spot_cents": avg_spot,
            "import_kwh": import_kwh,
            "import_cost_dollars": import_cost,
            "goodwe_export_kwh": round(goodwe_export, 2) if goodwe_export is not None else None,
            "load_kwh": round(load_kwh, 2) if load_kwh is not None else None,
            "solar_kwh": round(solar_kwh, 2) if solar_kwh is not None else None,
            "battery_discharge_kwh": round(battery_discharge, 2) if battery_discharge is not None else None,
            "blended_rate_cents": round(blended_rate_c, 2) if blended_rate_c is not None else None,
            "assumed_savings_dollars": assumed_savings,
        }
        result.append(row)

    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(result, f, indent=2)

    # Summary
    total_export_val = sum(r["export_value_dollars"] for r in result)
    total_import = sum(r["import_cost_dollars"] for r in result)
    n_feedin = sum(1 for r in result if r["feedin_kwh"] > 0.001)
    print(f"\nWrote {len(result)} days -> {DATA_FILE}")
    print(f"Export value total: ${total_export_val:.2f} across {n_feedin} days with feed-in")
    print(f"Import cost total:  ${total_import:.2f}")
    print("\nLast 6 days:")
    for r in result[-6:]:
        print(f"  {r['date']}: feedIn={r['feedin_kwh']}kWh expVal=${r['export_value_dollars']:.2f} (spot {r['avg_spot_cents']}c) imp=${r['import_cost_dollars']:.2f} sav=${r['assumed_savings_dollars']}")


if __name__ == "__main__":
    main()
