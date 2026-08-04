#!/usr/bin/env python3
"""Fetch Amber daily usage summaries and save to gary-pages data directory.

Backfills up to 90 days of daily Amber usage data (cost, grid import/export,
tariff breakdowns) into data/amber-daily.json.  Designed to run as part of
the energy dashboard pipeline.

Usage:
    python3 amber_daily_summary.py              # fetch last 90 days
    python3 amber_daily_summary.py 2026-08-01   # fetch from date onward
"""

import json, ssl, urllib.request, sys, os, re
from datetime import date, timedelta
from collections import defaultdict

# ── Paths ──
GARY_PAGES = os.path.expanduser("~/Projects/gary-pages")
DATA_FILE = os.path.join(GARY_PAGES, "data", "amber-daily.json")

# ── Credentials (from existing price alert script) ──
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

# ── SSL ──
def get_ssl_ctx():
    ctx = ssl.create_default_context()
    for p in ["/usr/local/etc/openssl@3/cert.pem", "/usr/local/etc/openssl/cert.pem", "/etc/ssl/cert.pem"]:
        if os.path.exists(p):
            ctx.load_verify_locations(p)
            break
    return ctx


def fetch_usage(start_str, end_str):
    """Fetch Amber usage for a date range (max 7 days). Returns list of intervals."""
    url = f"https://api.amber.com.au/v1/sites/{SITE_ID}/usage?startDate={start_str}&endDate={end_str}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {AMBER_TOKEN}",
        "Accept": "application/json"
    })
    try:
        with urllib.request.urlopen(req, timeout=30, context=get_ssl_ctx()) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} for {start_str} to {end_str}: {e.reason}")
        return []
    except Exception as e:
        print(f"  Error for {start_str} to {end_str}: {e}")
        return []


def aggregate_daily(intervals):
    """Aggregate Amber intervals into daily summaries.

    Returns dict: date_str -> {total_kwh, total_cost_cents, grid_import_kwh,
    grid_export_kwh, peak_kwh, peak_cost_cents, offPeak_kwh, offPeak_cost_cents,
    solarSponge_kwh, solarSponge_cost_cents}
    """
    days = defaultdict(lambda: {
        "total_kwh": 0, "total_cost_cents": 0,
        "grid_import_kwh": 0, "grid_export_kwh": 0,
        "peak_kwh": 0, "peak_cost_cents": 0,
        "offPeak_kwh": 0, "offPeak_cost_cents": 0,
        "solarSponge_kwh": 0, "solarSponge_cost_cents": 0,
        "intervals": 0
    })

    for item in intervals:
        d = item.get("date", "")
        if not d:
            continue
        ch_type = item.get("channelType", "")
        ch_id = item.get("channelIdentifier", "")
        kwh = item.get("kwh", 0)
        cost_cents = item.get("cost", 0)  # already in cents
        period = item.get("tariffInformation", {}).get("period", "unknown")

        day = days[d]
        day["intervals"] += 1

        if ch_type == "general" and ch_id == "E1":
            day["total_kwh"] += kwh
            day["total_cost_cents"] += cost_cents
            day["grid_import_kwh"] += kwh

            # Tariff breakdown (only for general:E1)
            if period in ("peak", "offPeak", "solarSponge"):
                day[f"{period}_kwh"] += kwh
                day[f"{period}_cost_cents"] += cost_cents

        elif ch_type == "feedIn":
            # Feed-in: positive kWh = energy exported to grid
            day["grid_export_kwh"] += kwh

    return days


def format_daily(days_dict):
    """Convert aggregated dict to sorted list of daily objects."""
    result = []
    for d_str in sorted(days_dict.keys()):
        info = days_dict[d_str]
        result.append({
            "date": d_str,
            "total_kwh": round(info["total_kwh"], 3),
            "total_cost_dollars": round(info["total_cost_cents"] / 100, 2),
            "grid_import_kwh": round(info["grid_import_kwh"], 3),
            "grid_export_kwh": round(info["grid_export_kwh"], 3),
            "peak_kwh": round(info["peak_kwh"], 3),
            "peak_cost_dollars": round(info["peak_cost_cents"] / 100, 2),
            "offPeak_kwh": round(info["offPeak_kwh"], 3),
            "offPeak_cost_dollars": round(info["offPeak_cost_cents"] / 100, 2),
            "solarSponge_kwh": round(info["solarSponge_kwh"], 3),
            "solarSponge_cost_dollars": round(info["solarSponge_cost_cents"] / 100, 2),
        })
    return result


def load_existing():
    """Load existing amber-daily.json if it exists."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return []


def merge_entries(existing, new_entries):
    """Merge new entries into existing, keyed by date. Newer data wins for the same date."""
    merged = {e["date"]: e for e in existing}
    for e in new_entries:
        merged[e["date"]] = e  # overwrite with fresher data
    return sorted(merged.values(), key=lambda x: x["date"])


def main():
    # Determine date range
    end_date = date.today() - timedelta(days=1)  # yesterday (Amber ~15h lag)
    
    if len(sys.argv) > 1:
        start_date = date.fromisoformat(sys.argv[1])
    else:
        start_date = end_date - timedelta(days=90)

    print(f"Fetching Amber daily summaries from {start_date} to {end_date}...")

    all_days = {}
    chunk_start = start_date

    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=6), end_date)  # 7-day window
        s_str = chunk_start.isoformat()
        e_str = chunk_end.isoformat()

        print(f"  {s_str} to {e_str}...", end=" ", flush=True)
        data = fetch_usage(s_str, e_str)

        if data:
            days = aggregate_daily(data)
            all_days.update(days)
            print(f"{len(days)} days, {len(data)} intervals")
        else:
            print("no data (likely outside 90-day window or meter not yet active)")

        chunk_start = chunk_end + timedelta(days=1)

    if not all_days:
        print("No Amber data retrieved. Check API token and site ID.")
        sys.exit(1)

    new_entries = format_daily(all_days)

    # Merge with existing
    existing = load_existing()
    merged = merge_entries(existing, new_entries)

    # Save
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(merged, f, indent=2)

    # Summary
    total_cost = sum(e["total_cost_dollars"] for e in merged)
    total_kwh = sum(e["total_kwh"] for e in merged)
    total_export = sum(e["grid_export_kwh"] for e in merged)
    print(f"\nSaved {len(merged)} days to {DATA_FILE}")
    print(f"Date range: {merged[0]['date']} to {merged[-1]['date']}")
    print(f"Total: {total_kwh:.1f} kWh imported · ${total_cost:.2f} cost · {total_export:.1f} kWh exported")

    # Show last 5 days
    print("\nLast 5 days:")
    for e in merged[-5:]:
        print(f"  {e['date']}: {e['total_kwh']:.1f} kWh · ${e['total_cost_dollars']:.2f} · import {e['grid_import_kwh']:.1f} · export {e['grid_export_kwh']:.1f}")


if __name__ == "__main__":
    main()
