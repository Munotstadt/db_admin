#!/usr/bin/env python3
"""
GitHub Usage Collector
----------------------
Ruft 1x täglich die aktuellen Verbrauchswerte der laufenden Abrechnungsperiode
für die GitHub-Organisation ab (Actions-Minuten, Shared Storage, Actions-Cache)
und hängt das Ergebnis an data/github_usage.csv an.

Nutzt die klassischen Org-Billing-Endpoints (funktionieren auf Free/Team-Plan
ohne Enterprise). Für den Zugriff wird ein Personal Access Token mit Scope
`admin:org` (classic) bzw. Fine-grained-Token mit "Administration" (read)
benötigt - der Standard-GITHUB_TOKEN aus Actions reicht dafür NICHT aus.

Benötigte Umgebungsvariablen (als GitHub Secret zu setzen):
  GH_BILLING_TOKEN -> PAT mit admin:org (read) Berechtigung
  GH_ORG           -> Organisation-Slug (z.B. "Munotstadt")

CSV-Spalten (Datumsformat: DD.MM.YYYY):
  Datum;TotalMinutesUsed;IncludedMinutes;PaidMinutesUsed;
  MinutesUbuntu;MinutesMacOS;MinutesWindows;
  StorageEstimateGB;CacheBytes
"""

import csv
import os
import sys
from datetime import datetime, timezone

import requests

API_BASE = "https://api.github.com"
CSV_PATH = os.path.join("data", "github_usage.csv")
CSV_HEADER = [
    "Datum", "TotalMinutesUsed", "IncludedMinutes", "PaidMinutesUsed",
    "MinutesUbuntu", "MinutesMacOS", "MinutesWindows",
    "StorageEstimateGB", "CacheBytes",
]
CSV_DELIMITER = ";"


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"FEHLER: Umgebungsvariable {name} fehlt.", file=sys.stderr)
        sys.exit(1)
    return value.strip()


def api_get(path: str, token: str) -> dict:
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"FEHLER {resp.status_code} bei {path}: {resp.text}", file=sys.stderr)
    resp.raise_for_status()
    return resp.json()


def load_existing_dates(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    dates = set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=CSV_DELIMITER)
        for row in reader:
            dates.add(row["Datum"])
    return dates


def main() -> None:
    token = get_env("GH_BILLING_TOKEN")
    org = get_env("GH_ORG")

    today_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")

    os.makedirs("data", exist_ok=True)
    existing_dates = load_existing_dates(CSV_PATH)
    file_exists = os.path.exists(CSV_PATH)

    if today_str in existing_dates:
        print(f"Übersprungen (bereits vorhanden): {today_str}")
        return

    actions = api_get(f"/orgs/{org}/settings/billing/actions", token)
    storage = api_get(f"/orgs/{org}/settings/billing/shared-storage", token)

    try:
        cache = api_get(f"/orgs/{org}/actions/cache/usage", token)
        cache_bytes = cache.get("total_active_caches_size_in_bytes", 0)
    except requests.HTTPError:
        print("Hinweis: Cache-Usage-Endpoint nicht verfügbar, setze 0.", file=sys.stderr)
        cache_bytes = 0

    breakdown = actions.get("minutes_used_breakdown", {})

    row = [
        today_str,
        actions.get("total_minutes_used", 0),
        actions.get("included_minutes", 0),
        actions.get("total_paid_minutes_used", 0),
        breakdown.get("UBUNTU", 0),
        breakdown.get("MACOS", 0),
        breakdown.get("WINDOWS", 0),
        storage.get("estimated_storage_for_month", 0),
        cache_bytes,
    ]

    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=CSV_DELIMITER)
        if not file_exists:
            writer.writerow(CSV_HEADER)
        writer.writerow(row)

    print(f"OK: {today_str} -> minutes={row[1]}/{row[2]} storage={row[7]}GB cache={row[8]}B")


if __name__ == "__main__":
    main()
