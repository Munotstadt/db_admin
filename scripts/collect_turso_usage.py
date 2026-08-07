#!/usr/bin/env python3
"""
Turso Usage Collector
----------------------
Ruft 1x täglich für JEDE Datenbank der Organisation die Usage-Statistiken
(rows_read, rows_written, bytes_synced, storage_bytes) für den VORTAG ab
und hängt das Ergebnis an data/turso_usage.csv an.

Benötigte Umgebungsvariablen (als GitHub Secrets zu setzen):
  TURSO_API_TOKEN   -> Turso Platform API Token (turso auth api-tokens mint <name>)
  TURSO_ORG_SLUG    -> Organisation- oder Account-Slug

CSV-Spalten (Datumsformat gemäss Munotstadt-Konvention: DD.MM.YYYY):
  Datum;Datenbank;RowsRead;RowsWritten;BytesSynced;StorageBytes
"""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

API_BASE = "https://api.turso.tech/v1"
CSV_PATH = os.path.join("data", "turso_usage.csv")
CSV_HEADER = ["Datum", "Datenbank", "RowsRead", "RowsWritten", "BytesSynced", "StorageBytes"]
CSV_DELIMITER = ";"


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"FEHLER: Umgebungsvariable {name} fehlt.", file=sys.stderr)
        sys.exit(1)
    # Schutz vor unsichtbaren Zeilenumbrüchen/Leerzeichen aus Copy-Paste in GitHub Secrets
    return value.strip()


def api_get(path: str, token: str, params: dict | None = None) -> dict:
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def list_databases(org: str, token: str) -> list[str]:
    data = api_get(f"/organizations/{org}/databases", token)
    return [db["name"] for db in data.get("databases", [])]


def get_usage(org: str, db_name: str, token: str, day_start: datetime, day_end: datetime) -> dict:
    params = {
        "from": day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": day_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    data = api_get(f"/organizations/{org}/databases/{db_name}/usage", token, params)
    return data["database"]["total"]


def load_existing_keys(path: str) -> set[tuple[str, str]]:
    """Liest bereits vorhandene (Datum, Datenbank)-Kombinationen, um Duplikate zu vermeiden."""
    if not os.path.exists(path):
        return set()
    keys = set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=CSV_DELIMITER)
        for row in reader:
            keys.add((row["Datum"], row["Datenbank"]))
    return keys


def main() -> None:
    token = get_env("TURSO_API_TOKEN")
    org = get_env("TURSO_ORG_SLUG")
    print(f"Verwende Org-Slug: '{org}' (Länge: {len(org)})")

    # Voller Vortag in UTC (00:00 bis 00:00), da der Cron einmal täglich läuft
    now_utc = datetime.now(timezone.utc)
    day_end = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = day_end - timedelta(days=1)
    datum_str = day_start.strftime("%d.%m.%Y")

    os.makedirs("data", exist_ok=True)
    existing_keys = load_existing_keys(CSV_PATH)
    file_exists = os.path.exists(CSV_PATH)

    databases = list_databases(org, token)
    if not databases:
        print("Keine Datenbanken gefunden.")
        return

    rows_to_write = []
    for db_name in databases:
        key = (datum_str, db_name)
        if key in existing_keys:
            print(f"Übersprungen (bereits vorhanden): {datum_str} / {db_name}")
            continue
        try:
            usage = get_usage(org, db_name, token, day_start, day_end)
        except requests.HTTPError as exc:
            print(f"FEHLER bei {db_name}: {exc}", file=sys.stderr)
            continue

        rows_to_write.append([
            datum_str,
            db_name,
            usage.get("rows_read", 0),
            usage.get("rows_written", 0),
            usage.get("bytes_synced", 0),
            usage.get("storage_bytes", 0),
        ])
        print(f"OK: {datum_str} / {db_name} -> {usage}")

    if not rows_to_write:
        print("Nichts Neues zu schreiben.")
        return

    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=CSV_DELIMITER)
        if not file_exists:
            writer.writerow(CSV_HEADER)
        writer.writerows(rows_to_write)

    print(f"{len(rows_to_write)} Zeile(n) an {CSV_PATH} angehängt.")


if __name__ == "__main__":
    main()
