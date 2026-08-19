# db_admin

Statisches GitHub-Pages-Dashboard zur Überwachung von Turso- und Neon-Datenbanken (Munotstadt), plus Admin-Werkzeuge für Ad-hoc-SQL-Befehle. Läuft komplett browserbasiert (iPad-tauglich), Datenerfassung via GitHub Actions Cron.

**Live-Dashboard:** `https://munotstadt.github.io/db_admin/`

## Was das Repo macht

- **Turso**: täglich Reads/Writes/Bytes-Synced/Storage pro Datenbank erfassen
- **Neon**: täglich Compute-/Storage-/Transfer-Werte pro Projekt erfassen
- **Dashboard** (`index.html`): Umschalter zwischen "Turso" und "Neon", mit Gesamt-Kacheln, Monats-Forecast (Turso) und Datenbank-/Projekt-Detailansicht
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

### 2. GitHub Pages aktivieren

Settings → Pages → Source: "Deploy from a branch" → Branch: `main`, Ordner `/ (root)`.

### 3. Workflows

Laufen automatisch täglich per Cron, können aber auch manuell unter **Actions** ausgelöst werden ("Run workflow"):

| Workflow | Zeitpunkt | Zweck |
|---|---|---|
| `collect-usage.yml` | 03:00 UTC | Turso-Usage aller Datenbanken abrufen, an `data/turso_usage.csv` anhängen |
| `collect-neon-usage.yml` | 03:05 UTC | Neon-Usage aller Projekte abrufen, an `data/neon_usage.csv` anhängen |
| `run-sql.yml` | manuell | Beliebiges SQL gegen eine Turso-Datenbank ausführen (Inputs: `database`, `sql`) |

## Dateistruktur

```
db_admin/
├── index.html                          # Dashboard (Turso/Neon-Umschalter)
├── data/
│   ├── turso_usage.csv                 # Tägliche Turso-Snapshots
│   └── neon_usage.csv                  # Tägliche Neon-Snapshots
├── scripts/
│   ├── collect_turso_usage.py          # Turso-Collector
│   ├── collect_neon_usage.py           # Neon-Collector
│   └── run_sql.py                      # Ad-hoc-SQL-Runner (Turso)
└── .github/workflows/
    ├── collect-usage.yml
    ├── collect-neon-usage.yml
    └── run-sql.yml
```

## CSV-Formate

Beide CSVs sind Semikolon-getrennt, Datumsformat `DD.MM.YYYY`.

**`turso_usage.csv`**: `Datum;Datenbank;RowsRead;RowsWritten;BytesSynced;StorageBytes`
Werte sind Tagesdeltas (ausser `StorageBytes`, ein Snapshot).

**`neon_usage.csv`**: `Datum;Projekt;ComputeTimeSeconds;ActiveTimeSeconds;WrittenDataBytes;DataTransferBytes;StorageBytes`
Werte sind kumuliert seit Beginn der aktuellen Neon-Abrechnungsperiode (von Neon selbst automatisch zurückgesetzt, kein manuelles Reset-Handling nötig).

## Dashboard-Logik

- **Turso-Ansicht**: Forecast-Kacheln (Ø letzte 30 Tage × 30.5, gegen feste Monatslimits), Gesamt-Kacheln mit %-Auslastung, Datenbank-Dropdown mit Kombi-Charts (Säule = Tag, Linie = Monat kumuliert, Reset am 1.)
- **Neon-Ansicht**: Gesamt-Kacheln über alle Projekte (noch ohne %-Limits — dafür müssten die Neon-Plan-Grenzwerte im Script hinterlegt werden), Projekt-Dropdown mit Verlaufs-Charts
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
