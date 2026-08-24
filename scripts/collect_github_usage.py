#!/usr/bin/env python3
"""
GitHub Usage Collector
----------------------
Ruft 1x täglich die Verbrauchswerte des VORTAGS für die GitHub-Organisation ab
und hängt das Ergebnis an data/github_usage.csv an.

WICHTIG: Die klassischen Billing-Endpoints (/orgs/{org}/settings/billing/actions
etc.) sind für Orgs auf der neuen "Enhanced Billing Platform" abgeschaltet
(liefern 404). Stattdessen wird der neue, tagesgenaue Endpoint genutzt:

  GET /organizations/{org}/settings/billing/usage?year=YYYY&month=M

Dieser liefert pro Tag/Produkt/SKU/Repo eine Zeile (usageItems), inkl.
tatsächlicher Kosten (netAmount, nach Abzug der im Plan enthaltenen Menge).
Siehe: https://docs.github.com/en/rest/billing/usage

Benötigte Umgebungsvariablen (als GitHub Secret zu setzen):
  GH_BILLING_TOKEN -> Fine-grained PAT mit "Administration" (read) auf die Org,
                      ODER Classic PAT mit admin:org
  GH_ORG           -> Organisation-Slug (z.B. "Munotstadt")

CSV-Spalten (Datumsformat: DD.MM.YYYY):
  Datum;ActionsMinutesLinux;ActionsMinutesMacOS;ActionsMinutesWindows;
  ActionsMinutesTotal;ActionsNetUSD;StorageGB;StorageNetUSD;CacheBytes
"""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

API_BASE = "https://api.github.com"
CSV_PATH = os.path.join("data", "github_usage.csv")
CSV_HEADER = [
    "Datum",
    "ActionsMinutesLinux", "ActionsMinutesMacOS", "ActionsMinutesWindows",
    "ActionsMinutesTotal", "ActionsNetUSD",
    "StorageGB", "StorageNetUSD",
    "CacheBytes",
]
CSV_DELIMITER = ";"


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"FEHLER: Umgebungsvariable {name} fehlt.", file=sys.stderr)
        sys.exit(1)
    return value.strip()


def api_get(path: str, token: str, params: dict | None = None) -> dict:
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        params=params,
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


def sku_bucket(sku: str) -> str:
    s = sku.lower()
    if "linux" in s or "ubuntu" in s:
        return "linux"
    if "macos" in s or "mac" in s:
        return "macos"
    if "windows" in s:
        return "windows"
    return "other"


def main() -> None:
    token = get_env("GH_BILLING_TOKEN")
    org = get_env("GH_ORG")

    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    target_date_str = yesterday.strftime("%Y-%m-%d")  # Format der API
    csv_date_str = yesterday.strftime("%d.%m.%Y")      # Munotstadt-Format

    os.makedirs("data", exist_ok=True)
    existing_dates = load_existing_dates(CSV_PATH)
    file_exists = os.path.exists(CSV_PATH)

    if csv_date_str in existing_dates:
        print(f"Übersprungen (bereits vorhanden): {csv_date_str}")
        return

    data = api_get(
        f"/organizations/{org}/settings/billing/usage",
        token,
        params={"year": yesterday.year, "month": yesterday.month},
    )
    items = [i for i in data.get("usageItems", []) if i.get("date") == target_date_str]

    minutes_by_os = {"linux": 0.0, "macos": 0.0, "windows": 0.0}
    actions_net = 0.0
    storage_gb = 0.0
    storage_net = 0.0

    for item in items:
        product = (item.get("product") or "").lower()
        qty = float(item.get("quantity") or 0)
        net = float(item.get("netAmount") or 0)

        if product == "actions":
            bucket = sku_bucket(item.get("sku", ""))
            if bucket in minutes_by_os:
                minutes_by_os[bucket] += qty
            actions_net += net
        elif "storage" in product or "packages" in product:
            storage_gb += qty
            storage_net += net

    try:
        cache = api_get(f"/orgs/{org}/actions/cache/usage", token)
        cache_bytes = cache.get("total_active_caches_size_in_bytes", 0)
    except requests.HTTPError:
        print("Hinweis: Cache-Usage-Endpoint nicht verfügbar, setze 0.", file=sys.stderr)
        cache_bytes = 0

    total_minutes = sum(minutes_by_os.values())

    row = [
        csv_date_str,
        round(minutes_by_os["linux"], 2),
        round(minutes_by_os["macos"], 2),
        round(minutes_by_os["windows"], 2),
        round(total_minutes, 2),
        round(actions_net, 4),
        round(storage_gb, 4),
        round(storage_net, 4),
        cache_bytes,
    ]

    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=CSV_DELIMITER)
        if not file_exists:
            writer.writerow(CSV_HEADER)
        writer.writerow(row)

    print(
        f"OK: {csv_date_str} -> Actions={total_minutes}min (${actions_net:.4f}) "
        f"Storage={storage_gb}GB (${storage_net:.4f}) Cache={cache_bytes}B "
        f"[{len(items)} usageItems verarbeitet]"
    )


if __name__ == "__main__":
    main()
