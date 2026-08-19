#!/usr/bin/env python3
"""
Neon Usage Collector
----------------------
Ruft 1x täglich für JEDES Neon-Projekt die aktuellen Verbrauchswerte der
laufenden Abrechnungsperiode ab und hängt das Ergebnis an data/neon_usage.csv an.

Nutzt den Free-Plan-tauglichen Endpoint GET /projects/{id} statt der
Consumption-History-API (die nur auf bezahlten Plänen verfügbar ist).
Diese Werte sind von Neon selbst bereits "seit Periodenbeginn" kumuliert -
kein manuelles Reset-Handling nötig (im Gegensatz zum Turso-Collector).

Benötigte Umgebungsvariable (als GitHub Secret zu setzen):
  NEON_API_KEY   -> Neon API Key (console.neon.tech -> Account Settings -> API Keys)

CSV-Spalten (Datumsformat: DD.MM.YYYY):
  Datum;Projekt;ComputeTimeSeconds;ActiveTimeSeconds;WrittenDataBytes;DataTransferBytes;StorageBytes
"""

import csv
import os
import sys
from datetime import datetime, timezone

import requests

API_BASE = "https://console.neon.tech/api/v2"
CSV_PATH = os.path.join("data", "neon_usage.csv")
CSV_HEADER = [
    "Datum", "Projekt", "ComputeTimeSeconds", "ActiveTimeSeconds",
    "WrittenDataBytes", "DataTransferBytes", "StorageBytes",
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
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def list_projects(token: str) -> list[dict]:
    data = api_get("/projects", token, params={"limit": 100})
    return data.get("projects", [])


def get_project_detail(project_id: str, token: str) -> dict:
    data = api_get(f"/projects/{project_id}", token)
    return data.get("project", {})


def get_project_storage_bytes(project_id: str, token: str) -> int:
    """Summiert logical_size über alle Branches (echte aktuelle Storage-Grösse,
    im Gegensatz zu data_storage_bytes_hour, das eine GB-Stunden-Grösse ist)."""
    data = api_get(f"/projects/{project_id}/branches", token)
    branches = data.get("branches", [])
    return sum(b.get("logical_size", 0) or 0 for b in branches)


def load_existing_keys(path: str) -> set[tuple[str, str]]:
    if not os.path.exists(path):
        return set()
    keys = set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=CSV_DELIMITER)
        for row in reader:
            keys.add((row["Datum"], row["Projekt"]))
    return keys


def main() -> None:
    token = get_env("NEON_API_KEY")

    today_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")

    os.makedirs("data", exist_ok=True)
    existing_keys = load_existing_keys(CSV_PATH)
    file_exists = os.path.exists(CSV_PATH)

    projects = list_projects(token)
    if not projects:
        print("Keine Neon-Projekte gefunden.")
        return

    rows_to_write = []
    for p in projects:
        project_id = p["id"]
        project_name = p.get("name", project_id)
        key = (today_str, project_name)
        if key in existing_keys:
            print(f"Übersprungen (bereits vorhanden): {today_str} / {project_name}")
            continue

        try:
            detail = get_project_detail(project_id, token)
            storage_bytes = get_project_storage_bytes(project_id, token)
        except requests.HTTPError as exc:
            print(f"FEHLER bei {project_name}: {exc}", file=sys.stderr)
            continue

        rows_to_write.append([
            today_str,
            project_name,
            detail.get("compute_time_seconds", 0),
            detail.get("active_time_seconds", 0),
            detail.get("written_data_bytes", 0),
            detail.get("data_transfer_bytes", 0),
            storage_bytes,
        ])
        print(f"OK: {today_str} / {project_name} -> compute={detail.get('compute_time_seconds')}s "
              f"active={detail.get('active_time_seconds')}s storage={storage_bytes}B")

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
