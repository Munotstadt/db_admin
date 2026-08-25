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

CSV-Spalten (Datumsformat: DD.MM.YYYY), Werte sind Tagesdeltas für den
VORTAG, ausser den mit "(Snapshot)" markierten Storage-Grössen:
  Datum;WorkersRequests;WorkersErrors;
  KVReads;KVWrites;KVStorageBytes;
  D1ReadQueries;D1WriteQueries;D1RowsRead;D1RowsWritten;D1StorageBytes;
  R2ClassAOps;R2ClassBOps;R2StorageBytes;
  ZoneRequests;ZoneBandwidthBytes

Hinweis: Die GraphQL-Datasets (workersInvocationsAdaptive,
kvOperationsAdaptiveGroups, kvStorageAdaptiveGroups,
d1AnalyticsAdaptiveGroups, r2OperationsAdaptiveGroups,
r2StorageAdaptiveGroups, httpRequests1dGroups) sind Teil der offiziellen
Cloudflare GraphQL Analytics API. Falls Cloudflare Feldnamen ändert, schlägt
der jeweilige Block einzeln fehl (siehe try/except je Sektion) und wird als
0 protokolliert, statt den ganzen Lauf abzubrechen.
"""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

API_URL = "https://api.cloudflare.com/client/v4/graphql"
CSV_PATH = os.path.join("data", "cloudflare_usage.csv")
CSV_HEADER = [
    "Datum",
    "WorkersRequests", "WorkersErrors",
    "KVReads", "KVWrites", "KVStorageBytes",
    "D1ReadQueries", "D1WriteQueries", "D1RowsRead", "D1RowsWritten", "D1StorageBytes",
    "R2ClassAOps", "R2ClassBOps", "R2StorageBytes",
    "ZoneRequests", "ZoneBandwidthBytes",
]
CSV_DELIMITER = ";"

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


def load_existing_dates(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    dates = set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=CSV_DELIMITER)
        for row in reader:
            dates.add(row["Datum"])
    return dates


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

    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    day = yesterday.strftime("%Y-%m-%d")
    csv_date_str = yesterday.strftime("%d.%m.%Y")

    os.makedirs("data", exist_ok=True)
    existing_dates = load_existing_dates(CSV_PATH)
    file_exists = os.path.exists(CSV_PATH)

    if csv_date_str in existing_dates:
        print(f"Übersprungen (bereits vorhanden): {csv_date_str}")
        return

    workers = safe("Workers", fetch_workers, token, account_id, day) or (0, 0)
    kv = safe("KV", fetch_kv, token, account_id, day) or (0, 0, 0)
    d1 = safe("D1", fetch_d1, token, account_id, day) or (0, 0, 0, 0, 0)
    r2 = safe("R2", fetch_r2, token, account_id, day) or (0, 0, 0)
    zone = (0, 0)
    if zone_id:
        zone = safe("Zone/CDN", fetch_zone, token, zone_id, day) or (0, 0)

    row = [
        csv_date_str,
        workers[0], workers[1],
        kv[0], kv[1], kv[2],
        d1[0], d1[1], d1[2], d1[3], d1[4],
        r2[0], r2[1], r2[2],
        zone[0], zone[1],
    ]

    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=CSV_DELIMITER)
        if not file_exists:
            writer.writerow(CSV_HEADER)
        writer.writerow(row)

    print(
        f"OK: {csv_date_str} -> Workers={workers[0]} Reqs, "
        f"KV={kv[0]}R/{kv[1]}W, D1={d1[2]}R/{d1[3]}W-Rows, "
        f"R2={r2[0]}A/{r2[1]}B Ops, Zone={zone[0]} Reqs"
    )


if __name__ == "__main__":
    main()
