// src/collectors.js
// Cloudflare-Usage-Collector (läuft als Cron Trigger im selben Worker wie
// Frontend/API). Turso und Neon sind entfernt.
//
// Benötigte Secrets (Worker -> Settings -> Variables and Secrets):
//   CF_API_TOKEN   (Account Analytics: Read, D1: Read; optional Zone Analytics: Read)
//   CF_ACCOUNT_ID
//   CF_ZONE_ID     (optional, für CDN/Zone-Requests)
//
// Der Collector ist idempotent und füllt Lücken selbst: bei jedem Lauf werden
// alle Tage seit dem letzten gespeicherten Tag bis gestern (UTC) nachgeholt,
// plus die letzten zwei Tage nochmals aktualisiert (Analytics-Nachlauf).

export const USAGE_COLS = [
  "Datum", "WorkersRequests", "WorkersErrors", "KVReads", "KVWrites", "KVStorageBytes",
  "D1ReadQueries", "D1WriteQueries", "D1RowsRead", "D1RowsWritten", "D1StorageBytes",
  "R2ClassAOps", "R2ClassBOps", "R2StorageBytes", "ZoneRequests", "ZoneBandwidthBytes",
];

const DAY_MS = 24 * 3600 * 1000;
const MAX_BACKFILL_DAYS = 60;

function pad(n) { return String(n).padStart(2, "0"); }
export function fmtDateUTC(d) { return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)}.${d.getUTCFullYear()}`; }
export function fmtDateISO(d) { return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`; }
function parseDatum(s) { // DD.MM.YYYY -> Date (UTC)
  const [dd, mm, yyyy] = s.split(".").map(Number);
  return new Date(Date.UTC(yyyy, mm - 1, dd));
}
function startOfDayUTC(d) { return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate())); }

// ---------- Run-Log (zeigt im Dashboard, ob der Collector läuft) ----------

export async function ensureRunsTable(env) {
  await env.DB.prepare(
    `CREATE TABLE IF NOT EXISTS collector_runs (
       RunID INTEGER PRIMARY KEY AUTOINCREMENT,
       RanAt TEXT NOT NULL DEFAULT (datetime('now')),
       TriggerType TEXT,
       Status TEXT,
       DaysCollected INTEGER DEFAULT 0,
       Message TEXT
     )`
  ).run();
}

export async function logRun(env, triggerType, status, days, message) {
  try {
    await ensureRunsTable(env);
    await env.DB.prepare(
      "INSERT INTO collector_runs (TriggerType, Status, DaysCollected, Message) VALUES (?, ?, ?, ?)"
    ).bind(triggerType, status, days || 0, (message || "").slice(0, 900)).run();
    // Log klein halten: nur die letzten 200 Läufe
    await env.DB.prepare(
      "DELETE FROM collector_runs WHERE RunID <= (SELECT MAX(RunID) - 200 FROM collector_runs)"
    ).run();
  } catch (e) {
    console.error("Run-Log fehlgeschlagen:", e.message);
  }
}

// ---------- Cloudflare GraphQL / REST ----------

const R2_CLASS_A = new Set([
  "PutObject", "CopyObject", "ListObjects", "ListBuckets", "PutBucket", "CreateMultipartUpload",
  "UploadPart", "UploadPartCopy", "CompleteMultipartUpload", "ListMultipartUploads", "ListParts",
  "PutBucketEncryption", "PutBucketCors", "PutBucketLifecycleConfiguration",
  "PutBucketEventNotificationConfig", "LifecycleStorageTiering", "LifecycleStorageTierTransition",
]);

async function cfGraphql(token, query, variables) {
  const res = await fetch("https://api.cloudflare.com/client/v4/graphql", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ query, variables }),
  });
  if (!res.ok) throw new Error(`GraphQL HTTP ${res.status}`);
  const data = await res.json();
  if (data.errors && data.errors.length) throw new Error(JSON.stringify(data.errors).slice(0, 300));
  return data.data;
}

async function currentD1StorageBytes(token, accountId) {
  let total = 0, page = 1, perPage = 100;
  for (;;) {
    const res = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${accountId}/d1/database?per_page=${perPage}&page=${page}`,
      { headers: { Authorization: `Bearer ${token}`, Accept: "application/json" } });
    if (!res.ok) throw new Error(`D1 REST HTTP ${res.status}`);
    const data = await res.json();
    const result = data.result || [];
    for (const db of result) total += db.file_size || db.size || 0;
    if (result.length < perPage) break;
    page++;
    if (page > 20) break;
  }
  return total;
}

// Sammelt alle Kennzahlen eines Tages. `warnings` erhält Teilfehler.
async function collectDay(env, dayISO, ctx) {
  const { token, accountId, zoneId, warnings } = ctx;
  const safe = async (label, fn) => {
    try { return await fn(); } catch (e) { warnings.push(`${dayISO} ${label}: ${e.message}`); return null; }
  };

  const workers = await safe("Workers", async () => {
    const q = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){workersInvocationsAdaptive(limit:10000,filter:{date:$date}){sum{requests errors}}}}}`;
    const data = await cfGraphql(token, q, { accountTag: accountId, date: dayISO });
    const rows = data.viewer.accounts[0].workersInvocationsAdaptive;
    return [rows.reduce((s, r) => s + r.sum.requests, 0), rows.reduce((s, r) => s + r.sum.errors, 0)];
  });

  const kv = await safe("KV", async () => {
    const opsQ = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){kvOperationsAdaptiveGroups(limit:10000,filter:{date:$date}){dimensions{actionType} sum{requests}}}}}`;
    const ops = await cfGraphql(token, opsQ, { accountTag: accountId, date: dayISO });
    const groups = ops.viewer.accounts[0].kvOperationsAdaptiveGroups;
    const reads = groups.filter(g => g.dimensions.actionType === "read").reduce((s, g) => s + g.sum.requests, 0);
    const writes = groups.filter(g => ["write", "delete"].includes(g.dimensions.actionType)).reduce((s, g) => s + g.sum.requests, 0);
    const stQ = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){kvStorageAdaptiveGroups(limit:1,filter:{date:$date},orderBy:[date_DESC]){dimensions{date} max{byteCount}}}}}`;
    const st = await cfGraphql(token, stQ, { accountTag: accountId, date: dayISO });
    const stGroups = st.viewer.accounts[0].kvStorageAdaptiveGroups;
    return [reads, writes, stGroups.length ? stGroups[0].max.byteCount : 0];
  });

  const d1 = await safe("D1", async () => {
    const q = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){d1AnalyticsAdaptiveGroups(limit:10000,filter:{date:$date}){sum{readQueries writeQueries rowsRead rowsWritten}}}}}`;
    const data = await cfGraphql(token, q, { accountTag: accountId, date: dayISO });
    const groups = data.viewer.accounts[0].d1AnalyticsAdaptiveGroups;
    return [
      groups.reduce((s, g) => s + g.sum.readQueries, 0),
      groups.reduce((s, g) => s + g.sum.writeQueries, 0),
      groups.reduce((s, g) => s + g.sum.rowsRead, 0),
      groups.reduce((s, g) => s + g.sum.rowsWritten, 0),
    ];
  });

  // D1-Storage: nur "gestern/heute" ist als Snapshot per REST verfügbar. Für
  // ältere (nachgeholte) Tage wird der letzte bekannte Wert übernommen.
  let d1Storage = ctx.lastD1Storage;
  if (dayISO === ctx.yesterdayISO) {
    const s = await safe("D1-Storage", () => currentD1StorageBytes(token, accountId));
    if (s != null) d1Storage = s;
  }
  ctx.lastD1Storage = d1Storage;

  const r2 = await safe("R2", async () => {
    const opsQ = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){r2OperationsAdaptiveGroups(limit:10000,filter:{date:$date}){dimensions{actionType} sum{requests}}}}}`;
    const ops = await cfGraphql(token, opsQ, { accountTag: accountId, date: dayISO });
    const groups = ops.viewer.accounts[0].r2OperationsAdaptiveGroups;
    const classA = groups.filter(g => R2_CLASS_A.has(g.dimensions.actionType)).reduce((s, g) => s + g.sum.requests, 0);
    const classB = groups.filter(g => !R2_CLASS_A.has(g.dimensions.actionType)).reduce((s, g) => s + g.sum.requests, 0);
    const stQ = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){r2StorageAdaptiveGroups(limit:1,filter:{date:$date},orderBy:[date_DESC]){dimensions{date} max{payloadSize}}}}}`;
    const st = await cfGraphql(token, stQ, { accountTag: accountId, date: dayISO });
    const stGroups = st.viewer.accounts[0].r2StorageAdaptiveGroups;
    return [classA, classB, stGroups.length ? stGroups[0].max.payloadSize : 0];
  });

  let zone = [0, 0];
  if (zoneId) {
    zone = await safe("Zone/CDN", async () => {
      const q = `query($zoneTag:string!,$date:string!){viewer{zones(filter:{zoneTag:$zoneTag}){httpRequests1dGroups(limit:1,filter:{date:$date}){sum{requests bytes}}}}}`;
      const data = await cfGraphql(token, q, { zoneTag: zoneId, date: dayISO });
      const groups = data.viewer.zones[0].httpRequests1dGroups;
      return groups.length ? [groups[0].sum.requests, groups[0].sum.bytes] : [0, 0];
    }) || [0, 0];
  }

  // Ohne Workers- UND D1-Daten ist der Tag nicht verwertbar (z. B. Token-/Rechtefehler):
  // nicht mit Nullen überschreiben, sondern als Fehler melden.
  if (workers === null && d1 === null) {
    throw new Error(`${dayISO}: weder Workers- noch D1-Analytics abrufbar (Token-Rechte prüfen)`);
  }
  const w = workers || [0, 0], k = kv || [0, 0, 0], d = d1 || [0, 0, 0, 0], r = r2 || [0, 0, 0];
  return {
    Datum: fmtDateUTC(new Date(dayISO + "T00:00:00Z")),
    WorkersRequests: w[0], WorkersErrors: w[1],
    KVReads: k[0], KVWrites: k[1], KVStorageBytes: k[2],
    D1ReadQueries: d[0], D1WriteQueries: d[1], D1RowsRead: d[2], D1RowsWritten: d[3], D1StorageBytes: d1Storage || 0,
    R2ClassAOps: r[0], R2ClassBOps: r[1], R2StorageBytes: r[2],
    ZoneRequests: zone[0], ZoneBandwidthBytes: zone[1],
  };
}

// opts.days: erzwingt die letzten N Tage (statt nur Lücken + 2 Tage Auffrischung)
export async function collectCloudflare(env, opts = {}) {
  const token = env.CF_API_TOKEN;
  const accountId = env.CF_ACCOUNT_ID;
  if (!token || !accountId) throw new Error("Secrets CF_API_TOKEN/CF_ACCOUNT_ID fehlen");

  const today = startOfDayUTC(new Date());
  const yesterday = new Date(today.getTime() - DAY_MS);

  const { results } = await env.DB.prepare("SELECT Datum, D1StorageBytes FROM cloudflare_usage").all();
  let last = null, lastD1Storage = 0;
  for (const r of results || []) {
    const d = parseDatum(r.Datum);
    if (!last || d > last) { last = d; lastD1Storage = Number(r.D1StorageBytes) || 0; }
  }

  let start;
  if (opts.days) start = new Date(yesterday.getTime() - (Math.min(opts.days, MAX_BACKFILL_DAYS) - 1) * DAY_MS);
  else if (!last) start = new Date(yesterday.getTime() - 34 * DAY_MS);
  else start = new Date(Math.min(last.getTime() + DAY_MS, yesterday.getTime() - DAY_MS)); // Lücke + 2 Tage Auffrischung
  const earliest = new Date(yesterday.getTime() - (MAX_BACKFILL_DAYS - 1) * DAY_MS);
  if (start < earliest) start = earliest;

  const ctx = {
    token, accountId, zoneId: env.CF_ZONE_ID, warnings: [],
    yesterdayISO: fmtDateISO(yesterday), lastD1Storage,
  };
  const placeholders = USAGE_COLS.map(() => "?").join(", ");
  const stmt = env.DB.prepare(`INSERT OR REPLACE INTO cloudflare_usage (${USAGE_COLS.join(", ")}) VALUES (${placeholders})`);

  let saved = 0;
  const errors = [];
  for (let t = start.getTime(); t <= yesterday.getTime(); t += DAY_MS) {
    const dayISO = fmtDateISO(new Date(t));
    try {
      const row = await collectDay(env, dayISO, ctx);
      await stmt.bind(...USAGE_COLS.map(c => row[c] ?? 0)).run();
      saved++;
    } catch (e) {
      errors.push(e.message);
      console.error("Collector-Tag fehlgeschlagen:", e.message);
    }
  }
  const msg = [errors.length ? `Fehler: ${errors.slice(0, 3).join(" | ")}` : null,
    ctx.warnings.length ? `Hinweise: ${ctx.warnings.slice(0, 3).join(" | ")}` : null]
    .filter(Boolean).join(" · ");
  if (saved === 0 && errors.length) throw new Error(msg);
  return { source: "cloudflare", from: fmtDateISO(start), to: ctx.yesterdayISO, saved, errors: errors.length, message: msg };
}
