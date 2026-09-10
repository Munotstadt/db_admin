#!/usr/bin/env python3
"""
Neon Usage Collector
----------------------
Ruft 1x täglich für JEDES Neon-Projekt die aktuellen Verbrauchswerte der
laufenden Abrechnungsperiode ab und schreibt das Ergebnis via API in die
Cloudflare-D1-Tabelle neon_usage (admin.munot.app/api/usage/neon).

Nutzt den Free-Plan-tauglichen Endpoint GET /projects/{id} statt der
Consumption-History-API (die nur auf bezahlten Plänen verfügbar ist).

Benötigte Umgebungsvariablen (als GitHub Secrets zu setzen):
  NEON_API_KEY   -> Neon API Key (console.neon.tech -> Account Settings -> API Keys)
  NEON_ORG_ID    -> (optional) Organisation-ID
  COLLECTOR_KEY  -> Shared Secret, muss mit der COLLECTOR_KEY Pages-Env-Variable
                    im db-admin Cloudflare-Pages-Projekt übereinstimmen
"""

import os
import sys
from datetime import datetime, timezone

import requests

API_BASE = "https://console.neon.tech/api/v2"
UPLOAD_URL = "https://admin.munot.app/api/usage/neon"


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"FEHLER: Umgebungsvariable {name} fehlt.", file=sys.stderr)
        sys.exit(1)
    return value.strip()


def get_env_optional(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value else None


def api_get(path: str, token: str, params: dict | None = None) -> dict:
    resp = requests.get(
        f"{API_BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params,
        timeout=30,
    )
    if not resp.ok:
        print(f"FEHLER {resp.status_code} bei {path}: {resp.text}", file=sys.stderr)
    resp.raise_for_status()
    return resp.json()


def list_projects(token: str, org_id: str | None) -> list[dict]:
    params = {"limit": 100}
    if org_id:
        params["org_id"] = org_id
    data = api_get("/projects", token, params=params)
    return data.get("projects", [])


def get_project_detail(project_id: str, token: str) -> dict:
    data = api_get(f"/projects/{project_id}", token)
    return data.get("project", {})


def get_project_storage_bytes(project_id: str, token: str) -> int:
    data = api_get(f"/projects/{project_id}/branches", token)
    branches = data.get("branches", [])
    return sum(b.get("logical_size", 0) or 0 for b in branches)


def upload_rows(rows: list[dict], collector_key: str) -> None:
    resp = requests.post(
        UPLOAD_URL,
        headers={"Content-Type": "application/json", "X-Collector-Key": collector_key},
        json={"rows": rows},
        timeout=30,
    )
    resp.raise_for_status()
    print("Upload-Antwort:", resp.json())


def main() -> None:
    token = get_env("NEON_API_KEY")
    org_id = get_env_optional("NEON_ORG_ID")
    collector_key = get_env("COLLECTOR_KEY")

    today_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")

    projects = list_projects(token, org_id)
    if not projects:
        print("Keine Neon-Projekte gefunden.")
        return

    rows_to_send = []
    for p in projects:
        project_id = p["id"]
        project_name = p.get("name", project_id)

        try:
            detail = get_project_detail(project_id, token)
            storage_bytes = get_project_storage_bytes(project_id, token)
        except requests.HTTPError as exc:
            print(f"FEHLER bei {project_name}: {exc}", file=sys.stderr)
            continue

        rows_to_send.append({
            "Datum": today_str,
            "Projekt": project_name,
            "ComputeTimeSeconds": detail.get("compute_time_seconds", 0),
            "ActiveTimeSeconds": detail.get("active_time_seconds", 0),
            "WrittenDataBytes": detail.get("written_data_bytes", 0),
            "DataTransferBytes": detail.get("data_transfer_bytes", 0),
            "StorageBytes": storage_bytes,
        })
        print(f"OK: {today_str} / {project_name} -> compute={detail.get('compute_time_seconds')}s "
              f"active={detail.get('active_time_seconds')}s storage={storage_bytes}B")

    if not rows_to_send:
        print("Nichts zu senden.")
        return

    upload_rows(rows_to_send, collector_key)


if __name__ == "__main__":
    main()
