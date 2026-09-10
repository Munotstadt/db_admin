#!/usr/bin/env python3
"""
Turso Usage Collector
----------------------
Ruft 1x täglich für JEDE Datenbank der Organisation die Usage-Statistiken
(rows_read, rows_written, bytes_synced, storage_bytes) für den VORTAG ab
und schreibt das Ergebnis via API in die Cloudflare-D1-Tabelle turso_usage
(admin.munot.app/api/usage/turso).

Benötigte Umgebungsvariablen (als GitHub Secrets zu setzen):
  TURSO_API_TOKEN   -> Turso Platform API Token (turso auth api-tokens mint <name>)
  TURSO_ORG_SLUG    -> Organisation- oder Account-Slug
  COLLECTOR_KEY     -> Shared Secret, muss mit der COLLECTOR_KEY Pages-Env-Variable
                       im db-admin Cloudflare-Pages-Projekt übereinstimmen
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

API_BASE = "https://api.turso.tech/v1"
UPLOAD_URL = "https://admin.munot.app/api/usage/turso"


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"FEHLER: Umgebungsvariable {name} fehlt.", file=sys.stderr)
        sys.exit(1)
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
    return [db["Name"] for db in data.get("databases", [])]


def _find_key_ci(node, target: str):
    from collections import deque
    queue = deque([node])
    while queue:
        current = queue.popleft()
        if isinstance(current, dict):
            for k, v in current.items():
                if k.lower() == target.lower():
                    return v
            for v in current.values():
                queue.append(v)
        elif isinstance(current, list):
            for item in current:
                queue.append(item)
    return None


def _extract_fields_from_dict(d: dict) -> dict:
    wanted = {"rows_read": 0, "rows_written": 0, "bytes_synced": 0, "storage_bytes": 0}
    lower_keys = {k.lower().replace("_", ""): k for k in d.keys()}
    for field in list(wanted.keys()):
        variant = field.replace("_", "")
        if variant in lower_keys:
            wanted[field] = d[lower_keys[variant]]
    return wanted


def _find_usage_fields(node) -> dict | None:
    wanted = {"rows_read": None, "rows_written": None, "bytes_synced": None, "storage_bytes": None}
    found_any = False

    def walk(obj):
        nonlocal found_any
        if isinstance(obj, dict):
            lower_keys = {k.lower(): k for k in obj.keys()}
            local_hit = False
            for field in wanted:
                key_variant = field.replace("_", "")
                for lk, orig_k in lower_keys.items():
                    if lk.replace("_", "") == key_variant:
                        wanted[field] = obj[orig_k]
                        local_hit = True
                        found_any = True
            if local_hit and all(v is not None for v in wanted.values()):
                return
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(node)
    return wanted if found_any else None


def get_usage(org: str, db_name: str, token: str, day_start: datetime, day_end: datetime) -> dict:
    params = {
        "from": day_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": day_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    data = api_get(f"/organizations/{org}/databases/{db_name}/usage", token, params)

    total_node = _find_key_ci(data, "total")
    if isinstance(total_node, dict):
        fields = _extract_fields_from_dict(total_node)
        if any(v != 0 for v in fields.values()) or "rowsread" in {
            k.lower().replace("_", "") for k in total_node.keys()
        }:
            print(f"DEBUG {db_name}: total-Knoten gefunden: {total_node}")
            return fields

    result = _find_usage_fields(data)
    if result is not None:
        print(f"DEBUG {db_name}: Fallback-Suche gefunden: {result}")
        return result

    print(f"WARNUNG: Konnte Usage-Felder für {db_name} nicht finden. Rohantwort: {data}", file=sys.stderr)
    return {"rows_read": 0, "rows_written": 0, "bytes_synced": 0, "storage_bytes": 0}


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
    token = get_env("TURSO_API_TOKEN")
    org = get_env("TURSO_ORG_SLUG")
    collector_key = get_env("COLLECTOR_KEY")
    print(f"Verwende Org-Slug: '{org}' (Länge: {len(org)})")

    now_utc = datetime.now(timezone.utc)
    day_end = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = day_end - timedelta(days=1)
    datum_str = day_start.strftime("%d.%m.%Y")

    databases = list_databases(org, token)
    if not databases:
        print("Keine Datenbanken gefunden.")
        return

    rows_to_send = []
    for db_name in databases:
        try:
            usage = get_usage(org, db_name, token, day_start, day_end)
        except requests.HTTPError as exc:
            print(f"FEHLER bei {db_name}: {exc}", file=sys.stderr)
            continue

        rows_to_send.append({
            "Datum": datum_str,
            "Datenbank": db_name,
            "RowsRead": usage.get("rows_read", 0),
            "RowsWritten": usage.get("rows_written", 0),
            "BytesSynced": usage.get("bytes_synced", 0),
            "StorageBytes": usage.get("storage_bytes", 0),
        })
        print(f"OK: {datum_str} / {db_name} -> {usage}")

    if not rows_to_send:
        print("Nichts zu senden.")
        return

    upload_rows(rows_to_send, collector_key)


if __name__ == "__main__":
    main()
