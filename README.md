# db_admin

Statisches GitHub-Pages-Dashboard zur Überwachung von Turso-, Neon- und Cloudflare-Ressourcen (Munotstadt), plus Admin-Werkzeuge für Ad-hoc-SQL-Befehle. Läuft komplett browserbasiert (iPad-tauglich), Datenerfassung via GitHub Actions Cron.

**Live-Dashboard:** `https://munotstadt.github.io/db_admin/`

Layout und CSS-Grundlage (`index.html`) folgen 1:1 dem Munotstadt-Suite-Stil aus [`splintmanager`](https://github.com/Munotstadt/splintmanager) (Topbar, `nav.tabs`, `.card.kpi`-Kacheln, `.section-title`, Farbtokens etc.), ohne dessen Google-Login-Gate — dieses Dashboard ist öffentlich lesbar.

## Was das Repo macht

- **Turso**: täglich Reads/Writes/Bytes-Synced/Storage pro Datenbank erfassen
- **Neon**: täglich Compute-/Storage-/Transfer-Werte pro Projekt erfassen
- **Cloudflare**: täglich Workers-Requests, KV-/D1-/R2-Nutzung (Tagesdelta) und Storage-Snapshots account-weit erfassen
- **Dashboard** (`index.html`): Umschalter zwischen "Turso", "Neon" und "Cloudflare", mit Gesamt-Kacheln, Monats-Forecast (Turso) und Datenbank-/Projekt-/Account-Detailansicht
- **Ad-hoc-SQL**: SQL-Befehle gegen eine Turso-Datenbank ausführen, ganz ohne lokales Terminal

## Setup

### 1. Secrets anlegen

Repo → Settings → Secrets and variables → Actions → folgende Repository Secrets anlegen:

| Secret | Wert | Woher |
|---|---|---|
| `TURSO_API_TOKEN` | Turso Platform API Token (org-weit, "All Groups") | Turso Dashboard → Settings → API Tokens |
| `TURSO_ORG_SLUG` | Turso Org-Slug | Turso Dashboard (URL bzw. `GET /v1/organizations`) |
| `NEON_API_KEY` | Neon API Key | Neon Dashboard → Account/Org Settings → API Keys |
| `NEON_ORG_ID` | Neon Organisation-ID (falls Account einer Org zugeordnet ist) | Neon Dashboard → Organization Settings |
| `CF_API_TOKEN` | Cloudflare API Token mit Berechtigung "Account Analytics: Read" (+ "Zone Analytics: Read" falls `CF_ZONE_ID` gesetzt) | Cloudflare Dashboard → My Profile → API Tokens → Create Token |
| `CF_ACCOUNT_ID` | Cloudflare Account-ID | Dashboard → beliebige Domain → rechte Seitenleiste, oder Workers & Pages → Overview |
| `CF_ZONE_ID` | (optional) Zone-ID einer Domain, für CDN/Zone-Requests | Dashboard → Domain → Overview → rechte Seitenleiste |

### 2. GitHub Pages aktivieren

Settings → Pages → Source: "Deploy from a branch" → Branch: `main`, Ordner `/ (root)`.

### 3. Workflows

Laufen automatisch täglich per Cron, können aber auch manuell unter **Actions** ausgelöst werden ("Run workflow"):

| Workflow | Zeitpunkt | Zweck |
|---|---|---|
| `collect-usage.yml` | 03:00 UTC | Turso-Usage aller Datenbanken abrufen, an `data/turso_usage.csv` anhängen |
| `collect-neon-usage.yml` | 03:05 UTC | Neon-Usage aller Projekte abrufen, an `data/neon_usage.csv` anhängen |
| `collect-cloudflare-usage.yml` | 03:10 UTC | Cloudflare Workers/KV/D1/R2-Nutzung abrufen, an `data/cloudflare_usage.csv` anhängen |
| `run-sql.yml` | manuell | Beliebiges SQL gegen eine Turso-Datenbank ausführen (Inputs: `database`, `sql`) |

> Ein früherer GitHub-Actions-Usage-Collector (`collect-github-usage.yml` / `collect_github_usage.py` / `data/github_usage.csv`) wurde entfernt, da er nicht zuverlässig funktionierte.

## Dateistruktur

```
db_admin/
├── index.html                          # Dashboard (Turso/Neon/Cloudflare-Umschalter)
├── data/
│   ├── turso_usage.csv                 # Tägliche Turso-Snapshots
│   ├── neon_usage.csv                  # Tägliche Neon-Snapshots
│   └── cloudflare_usage.csv            # Tägliche Cloudflare-Snapshots
├── scripts/
│   ├── collect_turso_usage.py          # Turso-Collector
│   ├── collect_neon_usage.py           # Neon-Collector
│   ├── collect_cloudflare_usage.py     # Cloudflare-Collector (Workers/KV/D1/R2)
│   └── run_sql.py                      # Ad-hoc-SQL-Runner (Turso)
└── .github/workflows/
    ├── collect-usage.yml
    ├── collect-neon-usage.yml
    ├── collect-cloudflare-usage.yml
    └── run-sql.yml
```

## CSV-Formate

Alle CSVs sind Semikolon-getrennt, Datumsformat `DD.MM.YYYY`.

**`turso_usage.csv`**: `Datum;Datenbank;RowsRead;RowsWritten;BytesSynced;StorageBytes`
Werte sind Tagesdeltas (ausser `StorageBytes`, ein Snapshot).

**`neon_usage.csv`**: `Datum;Projekt;ComputeTimeSeconds;ActiveTimeSeconds;WrittenDataBytes;DataTransferBytes;StorageBytes`
Werte sind kumuliert seit Beginn der aktuellen Neon-Abrechnungsperiode (von Neon selbst automatisch zurückgesetzt, kein manuelles Reset-Handling nötig).

**`cloudflare_usage.csv`**: `Datum;WorkersRequests;WorkersErrors;KVReads;KVWrites;KVStorageBytes;D1ReadQueries;D1WriteQueries;D1RowsRead;D1RowsWritten;D1StorageBytes;R2ClassAOps;R2ClassBOps;R2StorageBytes;ZoneRequests;ZoneBandwidthBytes`
Requests/Ops-Werte sind Tagesdeltas für den VORTAG, Storage-Werte (`*StorageBytes`) sind Snapshots (Stand am Ende des Vortags). Basiert auf der Cloudflare **GraphQL Analytics API** (`workersInvocationsAdaptive`, `kvOperationsAdaptiveGroups`, `kvStorageAdaptiveGroups`, `d1AnalyticsAdaptiveGroups`, `r2OperationsAdaptiveGroups`, `r2StorageAdaptiveGroups`, optional `httpRequests1dGroups` für eine Zone). Jeder Datenbereich (Workers/KV/D1/R2/Zone) wird einzeln abgefragt und fällt bei Fehlern (z.B. Feld nicht verfügbar, Zone nicht gesetzt) einzeln auf `0` zurück, statt den gesamten Lauf abzubrechen.

## Dashboard-Logik

Das Dashboard verwendet dieselben Layout-Bausteine wie `splintmanager`:

- **Topbar** (`header.topbar`, roter Unterstrich) mit Titel/Untertitel links, Status + "Aktualisieren"-Button rechts
- **`nav.tabs`**: Pill-artige Tab-Buttons (Turso / Neon / Cloudflare), aktiver Tab rot hervorgehoben
- **`.grid.cols-5` / `.card.kpi`**: Kennzahlen-Kacheln mit `label`/`value`/`delta` (+ optionalem Fortschrittsbalken bei Limit-Bezug), analog zum Splint-Dashboard-KPI-Grid
- **`.section-title`**: Abschnittsüberschrift mit optionalem `hint`-Text rechts
- **`.chart-card`**: gerahmte Detail-Karte pro Datenbank/Projekt mit KPI-Grid + Verlaufs-Charts (SVG-Sparklines/Kombi-Charts, eigenständig implementiert, nicht aus `splintmanager` übernommen)

Ansichten im Detail:

- **Turso**: Forecast-Kacheln (Ø letzte 30 Tage × 30.5, gegen feste Monatslimits), Gesamt-Kacheln mit %-Auslastung, Datenbank-Dropdown mit Kombi-Charts (Säule = Tag, Linie = Monat kumuliert, Reset am 1.)
- **Neon**: Gesamt-Kacheln über alle Projekte (noch ohne %-Limits — dafür müssten die Neon-Plan-Grenzwerte im Script hinterlegt werden), Projekt-Dropdown mit Verlaufs-Charts
- **Cloudflare**: Account-weite Kacheln (Workers/KV/D1/R2) mit %-Auslastung gegen die Free-Plan-Limits (`CF_LIMITS`-Konstante in `index.html`) sowie Verlaufs-Charts. R2-Ops-Limits gelten pro Monat, werden daher als Hochrechnung (Tageswert × 30.5) dargestellt, analog zum Turso-Forecast.
- Neue Datenbanken/Projekte erscheinen automatisch im jeweiligen Dropdown, sobald sie einmal in der CSV auftauchen — keine Code-Änderung nötig

## Ad-hoc-SQL ausführen

Für einmalige Admin-Befehle (Tabelle anlegen, Spalte ändern) ohne lokales Terminal:

1. Repo → Actions → "Run SQL on Turso Database" → Run workflow
2. `database`: Name der Ziel-Datenbank
3. `sql`: SQL-Statement(s), mit `;` getrennt
4. Ergebnis erscheint im Workflow-Log

Nutzt pro Lauf einen 5 Minuten gültigen, datenbankspezifischen Token (über die Turso Platform API gemintet) — kein dauerhafter DB-Token nötig.

## Offene Punkte

- Neon-Plan-Limits sind im Dashboard noch nicht hinterlegt (Turso-Limits: 500M Reads, 10M Writes, 3GB Sync, 5GB Storage, 50 DBs — siehe `LIMITS`-Konstante in `index.html`)
- Neon-Collector nutzt den Free-Plan-Endpoint (`GET /projects/{id}`); bei einem bezahlten Neon-Plan könnte auf die Consumption-History-API (`/consumption_history/v2/*`) umgestellt werden für echte Tages-Breakdowns statt Snapshots
- Cloudflare-Collector: Die verwendeten GraphQL-Dataset-/Feldnamen (`workersInvocationsAdaptive`, `kvOperationsAdaptiveGroups`, `kvStorageAdaptiveGroups`, `d1AnalyticsAdaptiveGroups`, `r2OperationsAdaptiveGroups`, `r2StorageAdaptiveGroups`) sollten beim ersten Lauf per `workflow_dispatch` geprüft werden (Actions-Log) — Cloudflare ändert GraphQL-Schemas gelegentlich ohne Versionierung. Bei Fehlern liefert das jeweilige Feld `0` statt den Lauf abzubrechen (siehe `safe()`-Wrapper im Script); im Actions-Log erscheint dann eine Zeile "Hinweis: … nicht verfügbar". `R2ClassAOps`/`R2ClassBOps`-Zuordnung basiert auf einer manuell gepflegten Liste (`R2_CLASS_A`) der Cloudflare-Operationstypen und sollte gegen die aktuelle [R2-Preisdoku](https://developers.cloudflare.com/r2/pricing/) abgeglichen werden.
- GitHub-Actions-Usage-Collector wurde entfernt (funktionierte nicht zuverlässig); falls später wieder benötigt, siehe Git-Historie vor diesem Commit.
