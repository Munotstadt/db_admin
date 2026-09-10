#!/usr/bin/env python3
"""
Cloudflare Usage Collector
--------------------------
Ruft 1x täglich für den VORTAG die Free-Tier-relevanten Verbrauchswerte über
die Cloudflare GraphQL Analytics API ab (Workers, KV, D1, R2, optional Zone/
CDN) und hängt das Ergebnis an data/cloudflare_usage.csv an.

Analog zum GitHub-Collector werden Tagesdeltas für den Vortag erfasst (nicht
kumuliert seit Periodenbeginn wie bei Neon), weil Cloudflares Free-Limits
(Workers-Requests, KV-Reads/Writes, D1-Reads/Writes) selbst auf Tagesbasis
gelten bzw. sich am saubersten so vergleichen lassen. Storage-Werte (KV, D1,
R2) sind Snapshots (aktueller Stand am Ende des Vortags).

Benötigte Umgebungsvariablen (als GitHub Secret zu setzen):
  CF_API_TOKEN   -> Cloudflare API Token mit Berechtigungen:
                     - Account: Account Analytics: Read
                     - Account: D1: Read (für die D1-Datenbankgrössen, da
                       diese nicht über die GraphQL Analytics API verfügbar
                       sind, siehe fetch_d1())
                     - Zone: Zone Analytics: Read, gescoped auf die
                       jeweilige Zone (nur falls CF_ZONE_ID gesetzt wird)
                     Erstellen unter: dash.cloudflare.com -> My Profile ->
                     API Tokens -> Create Token
  CF_ACCOUNT_ID  -> Cloudflare Account-ID (Dashboard -> rechte Seitenleiste
                     einer beliebigen Domain, oder Workers & Pages -> Overview)
  CF_ZONE_ID     -> (optional) Zone-ID einer Domain, falls CDN/Zone-Requests
                     mit erfasst werden sollen. Wenn nicht gesetzt, werden
                     ZoneRequests/ZoneBandwidthBytes als 0 geschrieben.
  COLLECTOR_KEY  -> Shared Secret, muss mit der COLLECTOR_KEY Pages-Env-Variable
                     im db-admin Cloudflare-Pages-Projekt übereinstimmen

Werte werden via admin.munot.app/api/usage/cloudflare in die D1-Tabelle
cloudflare_usage geschrieben (Tagesdeltas für den VORTAG, ausser den
Storage-Grössen, die Snapshots sind).

Hinweis: Die GraphQL-Datasets (workersInvocationsAdaptive,
kvOperationsAdaptiveGroups, kvStorageAdaptiveGroups,
d1AnalyticsAdaptiveGroups, r2OperationsAdaptiveGroups,
r2StorageAdaptiveGroups, httpRequests1dGroups) sind Teil der offiziellen
Cloudflare GraphQL Analytics API. Falls Cloudflare Feldnamen ändert, schlägt
der jeweilige Block einzeln fehl (siehe try/except je Sektion) und wird als
0 protokolliert, statt den ganzen Lauf abzubrechen.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

API_URL = "https://api.cloudflare.com/client/v4/graphql"
UPLOAD_URL = "https://admin.munot.app/api/usage/cloudflare"

# R2-Operationen: Class A = schreibend/listend, Class B = lesend
R2_CLASS_A = {
    "PutObject", "CopyObject", "ListObjects", "PutBucket", "CreateMultipartUpload",
    "UploadPart", "CompleteMultipartUpload", "ListMultipartUploads", "ListParts",
    "PutBucketEventNotificationConfig", "LifecycleStorageTiering",
}


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"FEHLER: Umgebungsvariable {name} fehlt.", file=sys.stderr)
        sys.exit(1)
    return value.strip()


def get_env_optional(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value else None


def graphql(token: str, query: str, variables: dict) -> dict:
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))
    return data["data"]


def upload_row(row: dict, collector_key: str) -> None:
    resp = requests.post(
        UPLOAD_URL,
        headers={"Content-Type": "application/json", "X-Collector-Key": collector_key},
        json={"rows": [row]},
        timeout=30,
    )
    resp.raise_for_status()
    print("Upload-Antwort:", resp.json())


def fetch_workers(token: str, account_id: str, day: str) -> tuple[int, int]:
    query = """
    query ($accountTag: string!, $date: string!) {
      viewer {
        accounts(filter: { accountTag: $accountTag }) {
          workersInvocationsAdaptive(
            limit: 10000
            filter: { date: $date }
          ) {
            sum { requests errors }
          }
        }
      }
    }
    """
    data = graphql(token, query, {"accountTag": account_id, "date": day})
    rows = data["viewer"]["accounts"][0]["workersInvocationsAdaptive"]
    requests_sum = sum(r["sum"]["requests"] for r in rows)
    errors_sum = sum(r["sum"]["errors"] for r in rows)
    return requests_sum, errors_sum


def fetch_kv(token: str, account_id: str, day: str) -> tuple[int, int, int]:
    ops_query = """
    query ($accountTag: string!, $date: string!) {
      viewer {
        accounts(filter: { accountTag: $accountTag }) {
          kvOperationsAdaptiveGroups(
            limit: 10000
            filter: { date: $date }
          ) {
            dimensions { actionType }
            sum { requests }
          }
        }
      }
    }
    """
    ops = graphql(token, ops_query, {"accountTag": account_id, "date": day})
    groups = ops["viewer"]["accounts"][0]["kvOperationsAdaptiveGroups"]
    reads = sum(g["sum"]["requests"] for g in groups if g["dimensions"]["actionType"] == "read")
    writes = sum(g["sum"]["requests"] for g in groups if g["dimensions"]["actionType"] in ("write", "delete"))

    storage_query = """
    query ($accountTag: string!, $date: string!) {
      viewer {
        accounts(filter: { accountTag: $accountTag }) {
          kvStorageAdaptiveGroups(
            limit: 1
            filter: { date: $date }
            orderBy: [date_DESC]
          ) {
            dimensions { date }
            max { byteCount }
          }
        }
      }
    }
    """
    storage = graphql(token, storage_query, {"accountTag": account_id, "date": day})
    storage_groups = storage["viewer"]["accounts"][0]["kvStorageAdaptiveGroups"]
    storage_bytes = storage_groups[0]["max"]["byteCount"] if storage_groups else 0
    return reads, writes, storage_bytes


def fetch_d1(token: str, account_id: str, day: str) -> tuple[int, int, int, int, int]:
    query = """
    query ($accountTag: string!, $date: string!) {
      viewer {
        accounts(filter: { accountTag: $accountTag }) {
          d1AnalyticsAdaptiveGroups(
            limit: 10000
            filter: { date: $date }
          ) {
            sum { readQueries writeQueries rowsRead rowsWritten }
          }
        }
      }
    }
    """
    data = graphql(token, query, {"accountTag": account_id, "date": day})
    groups = data["viewer"]["accounts"][0]["d1AnalyticsAdaptiveGroups"]
    read_q = sum(g["sum"]["readQueries"] for g in groups)
    write_q = sum(g["sum"]["writeQueries"] for g in groups)
    rows_read = sum(g["sum"]["rowsRead"] for g in groups)
    rows_written = sum(g["sum"]["rowsWritten"] for g in groups)

    # D1-Storage ist kein Feld in d1AnalyticsAdaptiveGroups (führte zu "unknown
    # field 'max'"). Stattdessen über die reguläre REST-API abrufen, die pro
    # Datenbank eine Dateigrösse liefert.
    storage = 0
    resp = requests.get(
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    databases = resp.json().get("result", []) or []
    for db in databases:
        storage += db.get("file_size") or db.get("size") or 0

    return read_q, write_q, rows_read, rows_written, storage


def fetch_r2(token: str, account_id: str, day: str) -> tuple[int, int, int]:
    ops_query = """
    query ($accountTag: string!, $date: string!) {
      viewer {
        accounts(filter: { accountTag: $accountTag }) {
          r2OperationsAdaptiveGroups(
            limit: 10000
            filter: { date: $date }
          ) {
            dimensions { actionType }
            sum { requests }
          }
        }
      }
    }
    """
    ops = graphql(token, ops_query, {"accountTag": account_id, "date": day})
    groups = ops["viewer"]["accounts"][0]["r2OperationsAdaptiveGroups"]
    class_a = sum(g["sum"]["requests"] for g in groups if g["dimensions"]["actionType"] in R2_CLASS_A)
    class_b = sum(g["sum"]["requests"] for g in groups if g["dimensions"]["actionType"] not in R2_CLASS_A)

    storage_query = """
    query ($accountTag: string!, $date: string!) {
      viewer {
        accounts(filter: { accountTag: $accountTag }) {
          r2StorageAdaptiveGroups(
            limit: 1
            filter: { date: $date }
            orderBy: [date_DESC]
          ) {
            dimensions { date }
            max { payloadSize }
          }
        }
      }
    }
    """
    storage = graphql(token, storage_query, {"accountTag": account_id, "date": day})
    storage_groups = storage["viewer"]["accounts"][0]["r2StorageAdaptiveGroups"]
    storage_bytes = storage_groups[0]["max"]["payloadSize"] if storage_groups else 0
    return class_a, class_b, storage_bytes


def fetch_zone(token: str, zone_id: str, day: str) -> tuple[int, int]:
    query = """
    query ($zoneTag: string!, $date: string!) {
      viewer {
        zones(filter: { zoneTag: $zoneTag }) {
          httpRequests1dGroups(
            limit: 1
            filter: { date: $date }
          ) {
            sum { requests bytes }
          }
        }
      }
    }
    """
    data = graphql(token, query, {"zoneTag": zone_id, "date": day})
    groups = data["viewer"]["zones"][0]["httpRequests1dGroups"]
    if not groups:
        return 0, 0
    return groups[0]["sum"]["requests"], groups[0]["sum"]["bytes"]


def safe(label: str, fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - einzelner Block darf den Lauf nicht stoppen
        print(f"Hinweis: {label} nicht verfügbar ({exc}), setze 0.", file=sys.stderr)
        return None


def main() -> None:
    token = get_env("CF_API_TOKEN")
    account_id = get_env("CF_ACCOUNT_ID")
    zone_id = get_env_optional("CF_ZONE_ID")
    collector_key = get_env("COLLECTOR_KEY")

    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    day = yesterday.strftime("%Y-%m-%d")
    csv_date_str = yesterday.strftime("%d.%m.%Y")

    workers = safe("Workers", fetch_workers, token, account_id, day) or (0, 0)
    kv = safe("KV", fetch_kv, token, account_id, day) or (0, 0, 0)
    d1 = safe("D1", fetch_d1, token, account_id, day) or (0, 0, 0, 0, 0)
    r2 = safe("R2", fetch_r2, token, account_id, day) or (0, 0, 0)
    zone = (0, 0)
    if zone_id:
        zone = safe("Zone/CDN", fetch_zone, token, zone_id, day) or (0, 0)

    row = {
        "Datum": csv_date_str,
        "WorkersRequests": workers[0], "WorkersErrors": workers[1],
        "KVReads": kv[0], "KVWrites": kv[1], "KVStorageBytes": kv[2],
        "D1ReadQueries": d1[0], "D1WriteQueries": d1[1], "D1RowsRead": d1[2],
        "D1RowsWritten": d1[3], "D1StorageBytes": d1[4],
        "R2ClassAOps": r2[0], "R2ClassBOps": r2[1], "R2StorageBytes": r2[2],
        "ZoneRequests": zone[0], "ZoneBandwidthBytes": zone[1],
    }

    upload_row(row, collector_key)

    print(
        f"OK: {csv_date_str} -> Workers={workers[0]} Reqs, "
        f"KV={kv[0]}R/{kv[1]}W, D1={d1[2]}R/{d1[3]}W-Rows, "
        f"R2={r2[0]}A/{r2[1]}B Ops, Zone={zone[0]} Reqs"
    )


if __name__ == "__main__":
    main()
