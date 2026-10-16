# db_admin

Admin-Dashboard der Munotstadt-Suite (`admin.munot.app`): überwacht die **Cloudflare-Nutzung** (Workers, D1, KV, R2) gegen die Kontingente von **Workers Paid + R2** und enthält das zentrale **Task-Log** (Improvements-Button). Turso und Neon sind entfernt.

Layout und CSS folgen `energy.munot.app` (Topbar, KPI-Kacheln, Cards, Tabs, Tooltip).

## Architektur

- **Worker `db-admin`** (`src/worker.js`) liefert API (`/api/...`) und Static Assets aus `public/`
- **D1 `db_admin`** (`78377bf1-f491-4879-bab0-d47a08605f77`): Tabellen `cloudflare_usage`, `improvement_tasks`, `collector_runs`
- **Cron** 18:05 und 02:00 Zürich-Zeit (ganzjährig, Crons `5 16,17 * * *` und `0 0,1 * * *` in UTC, `scheduled()` filtert auf die Zürcher Uhrzeit) → `scheduled()` → `collectCloudflare()` (`src/collectors.js`)
- Zugriffsschutz: Cloudflare Access (Google-Login)

## Collector

- Holt **alle fehlenden Tage** seit dem letzten Eintrag bis gestern (max. 60 Tage) und aktualisiert zusätzlich die letzten zwei Tage (Analytics-Nachlauf). Eine Pause des Crons wird dadurch automatisch aufgeholt.
- Schreibt keine Nullwerte, wenn weder Workers- noch D1-Analytics abrufbar sind (z. B. Token-Rechte) → Lauf wird als `error` protokolliert.
- Jeder Lauf landet in `collector_runs`; das Dashboard zeigt den letzten Lauf und warnt, wenn die Daten ≥ 2 Tage alt sind.
- D1-Storage ist per REST nur als aktueller Snapshot verfügbar; für nachgeholte Tage wird der letzte bekannte Wert übernommen.
- Manueller Lauf: `curl -X POST -H "X-Collector-Key: $KEY" "https://admin.munot.app/api/collector/run?days=45"`

### Secrets (Worker → Settings → Variables and Secrets)

| Secret | Zweck |
|---|---|
| `CF_API_TOKEN` | Account Analytics: Read + D1: Read (optional Zone Analytics: Read) |
| `CF_ACCOUNT_ID` | Account-ID |
| `CF_ZONE_ID` | optional, CDN-Requests einer Zone |
| `COLLECTOR_KEY` | für manuelle POST-Aufrufe |

## Kontingente (Workers Paid + R2)

Im Dashboard unter `PLAN` in `public/index.html` gepflegt (Stand Okt 2026). Überschreitung ist kein harter Stopp, sondern wird abgerechnet; das Dashboard rechnet eine Kostenprognose.

| Ressource | Inklusive / Monat | Überschuss |
|---|---|---|
| Workers Requests | 10 Mio | 0.30 USD / Mio |
| D1 Rows Read | 25 Mrd | 0.001 USD / Mio |
| D1 Rows Written | 50 Mio | 1.00 USD / Mio |
| D1 Storage | 5 GB | 0.75 USD / GB |
| KV Reads / Writes / Storage | 10 Mio / 1 Mio / 1 GB | 0.50 / 5.00 USD je Mio, 0.50 USD / GB |
| R2 Storage / Class A / Class B | 10 GB / 1 Mio / 10 Mio | 0.015 USD/GB, 4.50 / 0.36 USD je Mio |

Basis: 5 USD / Monat. `BILLING_DAY` (aktuell 24, Zyklus 24. bis 23.) bestimmt den Beginn der Abrechnungsperiode.

## Einmalige Aufräumarbeiten

- `drop_turso_neon.sql` im D1-Console ausführen, wenn die Turso-/Neon-Historie nicht mehr gebraucht wird (nicht umkehrbar).
- Secrets `TURSO_*`, `NEON_*` im Worker löschen.

## Dateien

```
public/index.html, tasks.html   Frontend
public/assets/…, assets/…       improvements-button.js (public/ = admin.munot.app, assets/ = GitHub Pages, von den Apps eingebunden)
src/worker.js, collectors.js    API + Cron
schema_usage.sql                D1-Schema
wrangler.toml                   Worker-Konfiguration
```
