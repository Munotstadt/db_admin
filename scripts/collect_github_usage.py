#!/usr/bin/env python3
"""
GitHub Usage Collector
----------------------
Ruft 1x täglich für JEDES Repo der Organisation die Workflow-Runs des VORTAGS
ab und summiert die abrechenbare Laufzeit (billable minutes) pro Runner-OS.
Hängt das Ergebnis an data/github_usage.csv an.

WICHTIG: Die Billing-Endpoints (weder die klassischen /orgs/{org}/settings/
billing/* noch die neuen /organizations/{org}/settings/billing/usage) sind für
diese Org per API nicht erreichbar - beide liefern 404. Laut GitHub-Doku sind
Billing-Endpoints nur für Orgs innerhalb eines Enterprise-Accounts bzw. mit
Zugriff auf die "Enhanced Billing Platform" verfügbar; eine normale Free/Team-
Org hat dort schlicht keinen API-Zugriff (nur über die Web-UI einsehbar).

Ausweg: Die Run-Timing-API (GET /repos/{owner}/{repo}/actions/runs/{run_id}/
timing) liefert pro Workflow-Run die exakte abrechenbare Laufzeit pro OS
(UBUNTU/MACOS/WINDOWS) - ganz ohne Billing-Berechtigung, nur mit normalem
Lesezugriff auf die Repos. Wir summieren das über alle Runs, die am Vortag
gestartet wurden.

Benötigte Umgebungsvariablen (als GitHub Secret zu setzen):
  GH_BILLING_TOKEN -> PAT mit "repo" Scope (classic) bzw. Fine-grained-Token
                      mit "Actions" (read) + "Contents" (read) auf alle Repos
                      der Org
  GH_ORG           -> Organisation-Slug (z.B. "Munotstadt")

CSV-Spalten (Datumsformat: DD.MM.YYYY):
  Datum;ActionsMinutesLinux;ActionsMinutesMacOS;ActionsMinutesWindows;
  ActionsMinutesTotal;RunsGezaehlt;RepoSizeKB;CacheBytes
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
    "ActionsMinutesTotal", "RunsGezaehlt", "RepoSizeKB", "CacheBytes",
]
CSV_DELIMITER = ";"
OS_KEYS = ["UBUNTU", "MACOS", "WINDOWS"]


def get_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"FEHLER: Umgebungsvariable {name} fehlt.", file=sys.stderr)
        sys.exit(1)
    return value.strip()


def api_get(path: str, token: str, params: dict | None = None) -> dict | None:
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
    if resp.status_code == 404:
        return None
    if not resp.ok:
        print(f"FEHLER {resp.status_code} bei {path}: {resp.text}", file=sys.stderr)
    resp.raise_for_status()
    return resp.json()


def api_get_paginated(path: str, token: str, list_key: str, params: dict | None = None) -> list[dict]:
    items: list[dict] = []
    page = 1
    params = dict(params or {})
    while True:
        params["per_page"] = 100
        params["page"] = page
        data = api_get(path, token, params=params)
        if data is None:
            break
        batch = data.get(list_key, data if isinstance(data, list) else [])
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


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

    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    day_start = yesterday.strftime("%Y-%m-%d")
    csv_date_str = yesterday.strftime("%d.%m.%Y")

    os.makedirs("data", exist_ok=True)
    existing_dates = load_existing_dates(CSV_PATH)
    file_exists = os.path.exists(CSV_PATH)

    if csv_date_str in existing_dates:
        print(f"Übersprungen (bereits vorhanden): {csv_date_str}")
        return

    # Repos direkt paginiert holen (einfache Variante, robuster als obiger Fallback-Versuch)
    repos: list[dict] = []
    page = 1
    while True:
        batch = requests.get(
            f"{API_BASE}/orgs/{org}/repos",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params={"type": "all", "per_page": 100, "page": page},
            timeout=30,
        )
        batch.raise_for_status()
        data = batch.json()
        if not data:
            break
        repos.extend(data)
        if len(data) < 100:
            break
        page += 1

    if not repos:
        print("Keine Repos in der Org gefunden.")
        return

    ms_by_os = {k: 0 for k in OS_KEYS}
    runs_counted = 0
    repo_size_kb = sum(r.get("size", 0) or 0 for r in repos)

    for repo in repos:
        owner_repo = repo["full_name"]
        runs = api_get_paginated(
            f"/repos/{owner_repo}/actions/runs",
            token,
            "workflow_runs",
            params={"created": day_start},
        )
        for run in runs:
            timing = api_get(f"/repos/{owner_repo}/actions/runs/{run['id']}/timing", token)
            if not timing:
                continue
            billable = timing.get("billable", {})
            for os_key in OS_KEYS:
                ms_by_os[os_key] += billable.get(os_key, {}).get("total_ms", 0) or 0
            runs_counted += 1

    minutes_by_os = {k: v / 60000 for k, v in ms_by_os.items()}
    total_minutes = sum(minutes_by_os.values())

    try:
        cache = api_get(f"/orgs/{org}/actions/cache/usage", token) or {}
        cache_bytes = cache.get("total_active_caches_size_in_bytes", 0)
    except requests.HTTPError:
        print("Hinweis: Cache-Usage-Endpoint nicht verfügbar, setze 0.", file=sys.stderr)
        cache_bytes = 0

    row = [
        csv_date_str,
        round(minutes_by_os["UBUNTU"], 2),
        round(minutes_by_os["MACOS"], 2),
        round(minutes_by_os["WINDOWS"], 2),
        round(total_minutes, 2),
        runs_counted,
        repo_size_kb,
        cache_bytes,
    ]

    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter=CSV_DELIMITER)
        if not file_exists:
            writer.writerow(CSV_HEADER)
        writer.writerow(row)

    print(
        f"OK: {csv_date_str} -> Actions={total_minutes:.2f}min "
        f"({runs_counted} Runs) RepoSize={repo_size_kb}KB Cache={cache_bytes}B"
    )


if __name__ == "__main__":
    main()
