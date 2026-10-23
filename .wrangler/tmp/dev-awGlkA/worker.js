var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// src/collectors.js
var USAGE_COLS = [
  "Datum",
  "WorkersRequests",
  "WorkersErrors",
  "KVReads",
  "KVWrites",
  "KVStorageBytes",
  "D1ReadQueries",
  "D1WriteQueries",
  "D1RowsRead",
  "D1RowsWritten",
  "D1StorageBytes",
  "R2ClassAOps",
  "R2ClassBOps",
  "R2StorageBytes",
  "ZoneRequests",
  "ZoneBandwidthBytes"
];
var DAY_MS = 24 * 3600 * 1e3;
var MAX_BACKFILL_DAYS = 60;
function pad(n) {
  return String(n).padStart(2, "0");
}
__name(pad, "pad");
function fmtDateUTC(d) {
  return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)}.${d.getUTCFullYear()}`;
}
__name(fmtDateUTC, "fmtDateUTC");
function fmtDateISO(d) {
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}
__name(fmtDateISO, "fmtDateISO");
function parseDatum(s) {
  const [dd, mm, yyyy] = s.split(".").map(Number);
  return new Date(Date.UTC(yyyy, mm - 1, dd));
}
__name(parseDatum, "parseDatum");
function startOfDayUTC(d) {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
}
__name(startOfDayUTC, "startOfDayUTC");
async function ensureRunsTable(env) {
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
__name(ensureRunsTable, "ensureRunsTable");
async function logRun(env, triggerType, status, days, message) {
  try {
    await ensureRunsTable(env);
    await env.DB.prepare(
      "INSERT INTO collector_runs (TriggerType, Status, DaysCollected, Message) VALUES (?, ?, ?, ?)"
    ).bind(triggerType, status, days || 0, (message || "").slice(0, 900)).run();
    await env.DB.prepare(
      "DELETE FROM collector_runs WHERE RunID <= (SELECT MAX(RunID) - 200 FROM collector_runs)"
    ).run();
  } catch (e) {
    console.error("Run-Log fehlgeschlagen:", e.message);
  }
}
__name(logRun, "logRun");
var R2_CLASS_A = /* @__PURE__ */ new Set([
  "PutObject",
  "CopyObject",
  "ListObjects",
  "ListBuckets",
  "PutBucket",
  "CreateMultipartUpload",
  "UploadPart",
  "UploadPartCopy",
  "CompleteMultipartUpload",
  "ListMultipartUploads",
  "ListParts",
  "PutBucketEncryption",
  "PutBucketCors",
  "PutBucketLifecycleConfiguration",
  "PutBucketEventNotificationConfig",
  "LifecycleStorageTiering",
  "LifecycleStorageTierTransition"
]);
async function cfGraphql(token, query, variables) {
  const res = await fetch("https://api.cloudflare.com/client/v4/graphql", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ query, variables })
  });
  if (!res.ok) throw new Error(`GraphQL HTTP ${res.status}`);
  const data = await res.json();
  if (data.errors && data.errors.length) throw new Error(JSON.stringify(data.errors).slice(0, 300));
  return data.data;
}
__name(cfGraphql, "cfGraphql");
async function currentD1StorageBytes(token, accountId) {
  let total = 0, page = 1, perPage = 100;
  for (; ; ) {
    const res = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${accountId}/d1/database?per_page=${perPage}&page=${page}`,
      { headers: { Authorization: `Bearer ${token}`, Accept: "application/json" } }
    );
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
__name(currentD1StorageBytes, "currentD1StorageBytes");
async function collectDay(env, dayISO, ctx) {
  const { token, accountId, zoneId, warnings } = ctx;
  const safe = /* @__PURE__ */ __name(async (label, fn) => {
    try {
      return await fn();
    } catch (e) {
      warnings.push(`${dayISO} ${label}: ${e.message}`);
      return null;
    }
  }, "safe");
  const workers = await safe("Workers", async () => {
    const q = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){workersInvocationsAdaptive(limit:10000,filter:{date:$date}){sum{requests errors}}}}}`;
    const data = await cfGraphql(token, q, { accountTag: accountId, date: dayISO });
    const rows = data.viewer.accounts[0].workersInvocationsAdaptive;
    return [rows.reduce((s, r3) => s + r3.sum.requests, 0), rows.reduce((s, r3) => s + r3.sum.errors, 0)];
  });
  const kv = await safe("KV", async () => {
    const opsQ = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){kvOperationsAdaptiveGroups(limit:10000,filter:{date:$date}){dimensions{actionType} sum{requests}}}}}`;
    const ops = await cfGraphql(token, opsQ, { accountTag: accountId, date: dayISO });
    const groups = ops.viewer.accounts[0].kvOperationsAdaptiveGroups;
    const reads = groups.filter((g) => g.dimensions.actionType === "read").reduce((s, g) => s + g.sum.requests, 0);
    const writes = groups.filter((g) => ["write", "delete"].includes(g.dimensions.actionType)).reduce((s, g) => s + g.sum.requests, 0);
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
      groups.reduce((s, g) => s + g.sum.rowsWritten, 0)
    ];
  });
  let d1Storage = ctx.lastD1Storage;
  if (dayISO === ctx.yesterdayISO) {
    const s = await safe("D1-Storage", () => currentD1StorageBytes(token, accountId));
    if (s != null) d1Storage = s;
  }
  ctx.lastD1Storage = d1Storage;
  const r22 = await safe("R2", async () => {
    const opsQ = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){r2OperationsAdaptiveGroups(limit:10000,filter:{date:$date}){dimensions{actionType} sum{requests}}}}}`;
    const ops = await cfGraphql(token, opsQ, { accountTag: accountId, date: dayISO });
    const groups = ops.viewer.accounts[0].r2OperationsAdaptiveGroups;
    const classA = groups.filter((g) => R2_CLASS_A.has(g.dimensions.actionType)).reduce((s, g) => s + g.sum.requests, 0);
    const classB = groups.filter((g) => !R2_CLASS_A.has(g.dimensions.actionType)).reduce((s, g) => s + g.sum.requests, 0);
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
  if (workers === null && d1 === null) {
    throw new Error(`${dayISO}: weder Workers- noch D1-Analytics abrufbar (Token-Rechte pr\xFCfen)`);
  }
  const w = workers || [0, 0], k = kv || [0, 0, 0], d = d1 || [0, 0, 0, 0], r = r22 || [0, 0, 0];
  return {
    Datum: fmtDateUTC(/* @__PURE__ */ new Date(dayISO + "T00:00:00Z")),
    WorkersRequests: w[0],
    WorkersErrors: w[1],
    KVReads: k[0],
    KVWrites: k[1],
    KVStorageBytes: k[2],
    D1ReadQueries: d[0],
    D1WriteQueries: d[1],
    D1RowsRead: d[2],
    D1RowsWritten: d[3],
    D1StorageBytes: d1Storage || 0,
    R2ClassAOps: r[0],
    R2ClassBOps: r[1],
    R2StorageBytes: r[2],
    ZoneRequests: zone[0],
    ZoneBandwidthBytes: zone[1]
  };
}
__name(collectDay, "collectDay");
async function collectCloudflare(env, opts = {}) {
  const token = env.CF_API_TOKEN;
  const accountId = env.CF_ACCOUNT_ID;
  if (!token || !accountId) throw new Error("Secrets CF_API_TOKEN/CF_ACCOUNT_ID fehlen");
  const today = startOfDayUTC(/* @__PURE__ */ new Date());
  const yesterday = new Date(today.getTime() - DAY_MS);
  const { results } = await env.DB.prepare("SELECT Datum, D1StorageBytes FROM cloudflare_usage").all();
  let last = null, lastD1Storage = 0;
  for (const r of results || []) {
    const d = parseDatum(r.Datum);
    if (!last || d > last) {
      last = d;
      lastD1Storage = Number(r.D1StorageBytes) || 0;
    }
  }
  let start;
  if (opts.days) start = new Date(yesterday.getTime() - (Math.min(opts.days, MAX_BACKFILL_DAYS) - 1) * DAY_MS);
  else if (!last) start = new Date(yesterday.getTime() - 34 * DAY_MS);
  else start = new Date(Math.min(last.getTime() + DAY_MS, yesterday.getTime() - DAY_MS));
  const earliest = new Date(yesterday.getTime() - (MAX_BACKFILL_DAYS - 1) * DAY_MS);
  if (start < earliest) start = earliest;
  const ctx = {
    token,
    accountId,
    zoneId: env.CF_ZONE_ID,
    warnings: [],
    yesterdayISO: fmtDateISO(yesterday),
    lastD1Storage
  };
  const placeholders = USAGE_COLS.map(() => "?").join(", ");
  const stmt = env.DB.prepare(`INSERT OR REPLACE INTO cloudflare_usage (${USAGE_COLS.join(", ")}) VALUES (${placeholders})`);
  let saved = 0;
  const errors = [];
  for (let t = start.getTime(); t <= yesterday.getTime(); t += DAY_MS) {
    const dayISO = fmtDateISO(new Date(t));
    try {
      const row = await collectDay(env, dayISO, ctx);
      await stmt.bind(...USAGE_COLS.map((c) => row[c] ?? 0)).run();
      saved++;
    } catch (e) {
      errors.push(e.message);
      console.error("Collector-Tag fehlgeschlagen:", e.message);
    }
  }
  const msg = [
    errors.length ? `Fehler: ${errors.slice(0, 3).join(" | ")}` : null,
    ctx.warnings.length ? `Hinweise: ${ctx.warnings.slice(0, 3).join(" | ")}` : null
  ].filter(Boolean).join(" \xB7 ");
  if (saved === 0 && errors.length) throw new Error(msg);
  return { source: "cloudflare", from: fmtDateISO(start), to: ctx.yesterdayISO, saved, errors: errors.length, message: msg };
}
__name(collectCloudflare, "collectCloudflare");

// src/ram.js
var DAY = 864e5;
var RANGES = {
  1: { ms: 1 * DAY, raw: true },
  // 24 h aus den Rohdaten (5-Minuten-Raster)
  7: { ms: 7 * DAY, raw: false },
  // ab hier aus den Stundenwerten
  30: { ms: 30 * DAY, raw: false },
  0: { ms: null, raw: false }
  // alles
};
var iso = /* @__PURE__ */ __name((t) => new Date(t).toISOString().replace(/\.\d{3}Z$/, "Z"), "iso");
var r2 = /* @__PURE__ */ __name((v) => v == null ? null : Math.round(v * 100) / 100, "r2");
var lo = /* @__PURE__ */ __name((a, b) => a == null ? b : b == null ? a : Math.min(a, b), "lo");
var hi = /* @__PURE__ */ __name((a, b) => a == null ? b : b == null ? a : Math.max(a, b), "hi");
var json = /* @__PURE__ */ __name((obj, status = 200, extra = {}) => new Response(JSON.stringify(obj), {
  status,
  headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...extra }
}), "json");
async function handleRam(url, env) {
  const q = url.searchParams.get("days");
  const days = q !== null && Object.hasOwn(RANGES, q) ? Number(q) : 7;
  const cfg = RANGES[days];
  const since = cfg.ms ? iso(Date.now() - cfg.ms) : null;
  let rows;
  if (cfg.raw) {
    const r = await env.DB.prepare(
      "SELECT ts, source, used_mb, total_mb, avail_mb, swap_mb, mem_pct FROM nas_ram_monitor WHERE ts >= ?1 ORDER BY ts"
    ).bind(since).all();
    rows = r.results.map((x) => ({
      ts: x.ts,
      source: x.source,
      n: 1,
      sum_used: x.used_mb,
      avg_used: x.used_mb,
      max_used: x.used_mb,
      min_avail: x.avail_mb,
      max_swap: x.swap_mb,
      total_mb: x.total_mb,
      pct: x.mem_pct,
      last_ts: x.ts,
      last_used: x.used_mb,
      last_avail: x.avail_mb,
      last_swap: x.swap_mb
    }));
  } else {
    const sql = "SELECT hour, source, n, sum_used, max_used, min_avail, max_swap, total_mb, sum_pct, last_ts, last_used, last_avail, last_swap FROM nas_ram_monitor_hourly" + (since ? " WHERE hour >= ?1" : "") + " ORDER BY hour";
    const stmt = env.DB.prepare(sql);
    const r = await (since ? stmt.bind(since.slice(0, 13) + ":00:00Z") : stmt).all();
    rows = r.results.map((x) => ({
      ts: x.hour,
      source: x.source,
      n: x.n,
      sum_used: x.sum_used,
      avg_used: x.sum_used / x.n,
      max_used: x.max_used,
      min_avail: x.min_avail,
      max_swap: x.max_swap,
      total_mb: x.total_mb,
      pct: x.sum_pct == null ? null : x.sum_pct / x.n,
      last_ts: x.last_ts,
      last_used: x.last_used,
      last_avail: x.last_avail,
      last_swap: x.last_swap
    }));
  }
  const sys = { min_avail: null, max_swap: null, total: null, last_used: null, last_avail: null, last_swap: null };
  const acc = /* @__PURE__ */ new Map();
  let lastTs = null, runs = 0, all = 0;
  for (const x of rows) {
    all += x.n;
    if (x.last_ts && (!lastTs || x.last_ts > lastTs)) lastTs = x.last_ts;
    if (x.source === "system") {
      runs += x.n;
      sys.min_avail = lo(sys.min_avail, x.min_avail);
      sys.max_swap = hi(sys.max_swap, x.max_swap);
      sys.total = x.total_mb ?? sys.total;
      sys.last_used = x.last_used;
      sys.last_avail = x.last_avail;
      sys.last_swap = x.last_swap;
    } else {
      const a = acc.get(x.source) || { source: x.source, sum: 0, n: 0, max: 0, last: null };
      a.sum += x.sum_used;
      a.n += x.n;
      a.max = Math.max(a.max, x.max_used);
      a.last = x.last_used;
      acc.set(x.source, a);
    }
  }
  const stats = [...acc.values()].map((a) => ({ source: a.source, avg_mb: r2(a.sum / a.n), max_mb: r2(a.max), last_mb: r2(a.last) })).sort((a, b) => b.avg_mb - a.avg_mb);
  for (const k of Object.keys(sys)) sys[k] = r2(sys[k]);
  return json({
    days,
    bucket: cfg.raw ? "5 Min (Rohdaten)" : "1 Stunde",
    rows: rows.map((x) => ({
      ts: x.ts,
      source: x.source,
      used_mb: r2(x.avg_used),
      total_mb: r2(x.total_mb),
      avail_mb: r2(x.min_avail),
      swap_mb: r2(x.max_swap),
      mem_pct: r2(x.pct)
    })),
    stats,
    system: sys,
    span: { first: rows.length ? rows[0].ts : null, last: lastTs, n: runs || all }
  });
}
__name(handleRam, "handleRam");

// src/worker.js
var ALLOWED_ORIGINS = [
  "https://munotstadt.github.io",
  "https://admin.munot.app",
  "https://energy.munot.app",
  "https://splint.munot.app",
  "https://meteo.munot.app",
  "https://52b.munot.app"
];
function corsHeaders(request) {
  const origin = request.headers.get("Origin");
  const allowOrigin = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];
  return {
    "Access-Control-Allow-Origin": allowOrigin,
    "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Credentials": "true",
    "Vary": "Origin"
  };
}
__name(corsHeaders, "corsHeaders");
function json2(data, status, cors) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...cors || {} }
  });
}
__name(json2, "json");
var USAGE_TABLES = {
  cloudflare: { table: "cloudflare_usage", cols: USAGE_COLS }
};
var ORDER_BY_DATUM = "substr(Datum,7,4) || substr(Datum,4,2) || substr(Datum,1,2)";
async function handleTasksCollection(request, env, cors) {
  if (request.method === "GET") {
    const url = new URL(request.url);
    const status = url.searchParams.get("status");
    const stmt = status ? env.DB.prepare("SELECT * FROM improvement_tasks WHERE Status = ? ORDER BY CreatedAt DESC").bind(status) : env.DB.prepare("SELECT * FROM improvement_tasks ORDER BY CreatedAt DESC");
    const { results } = await stmt.all();
    return json2(results, 200, cors);
  }
  if (request.method === "POST") {
    const body = await request.json();
    const { sourceApp, sourceUrl, title, note, priority } = body;
    if (!sourceApp || !title) return json2({ error: "sourceApp und title sind Pflicht" }, 400, cors);
    const result = await env.DB.prepare(
      `INSERT INTO improvement_tasks (SourceApp, SourceUrl, Title, Note, Priority) VALUES (?, ?, ?, ?, ?)`
    ).bind(sourceApp, sourceUrl || null, title, note || null, priority || "normal").run();
    return json2({ ok: true, TaskID: result.meta.last_row_id }, 201, cors);
  }
  return json2({ error: "method not allowed" }, 405, cors);
}
__name(handleTasksCollection, "handleTasksCollection");
async function handleTaskItem(request, env, cors, id) {
  if (request.method === "PATCH") {
    const body = await request.json();
    const fields = [];
    const values = [];
    for (const [key, col] of [["title", "Title"], ["note", "Note"], ["status", "Status"], ["priority", "Priority"]]) {
      if (body[key] !== void 0) {
        fields.push(`${col} = ?`);
        values.push(body[key]);
      }
    }
    if (body.status === "done") {
      fields.push("CompletedAt = ?");
      values.push((/* @__PURE__ */ new Date()).toISOString());
    }
    fields.push("UpdatedAt = ?");
    values.push((/* @__PURE__ */ new Date()).toISOString());
    values.push(id);
    if (fields.length === 1) return json2({ error: "nichts zu updaten" }, 400, cors);
    await env.DB.prepare(`UPDATE improvement_tasks SET ${fields.join(", ")} WHERE TaskID = ?`).bind(...values).run();
    return json2({ ok: true }, 200, cors);
  }
  if (request.method === "DELETE") {
    await env.DB.prepare("DELETE FROM improvement_tasks WHERE TaskID = ?").bind(id).run();
    return json2({ ok: true }, 200, cors);
  }
  return json2({ error: "method not allowed" }, 405, cors);
}
__name(handleTaskItem, "handleTaskItem");
async function handleUsage(request, env, source) {
  const cfg = USAGE_TABLES[source];
  if (!cfg) return json2({ error: "unbekannte source" }, 404);
  if (request.method === "GET") {
    const { results } = await env.DB.prepare(`SELECT * FROM ${cfg.table} ORDER BY ${ORDER_BY_DATUM} ASC`).all();
    return json2(results);
  }
  if (request.method === "POST") {
    const key = request.headers.get("X-Collector-Key");
    if (!key || key !== env.COLLECTOR_KEY) return json2({ error: "unauthorized" }, 401);
    const body = await request.json();
    const rows = Array.isArray(body.rows) ? body.rows : [body];
    if (!rows.length) return json2({ error: "keine Zeilen \xFCbergeben" }, 400);
    const placeholders = cfg.cols.map(() => "?").join(", ");
    const stmt = env.DB.prepare(`INSERT OR IGNORE INTO ${cfg.table} (${cfg.cols.join(", ")}) VALUES (${placeholders})`);
    let inserted = 0;
    for (const row of rows) {
      const values = cfg.cols.map((c) => row[c] ?? 0);
      const res = await stmt.bind(...values).run();
      if (res.meta.changes > 0) inserted++;
    }
    return json2({ ok: true, inserted, received: rows.length });
  }
  return json2({ error: "method not allowed" }, 405);
}
__name(handleUsage, "handleUsage");
async function handleCollectorStatus(env) {
  await ensureRunsTable(env);
  const { results } = await env.DB.prepare(
    "SELECT RunID, RanAt, TriggerType, Status, DaysCollected, Message FROM collector_runs ORDER BY RunID DESC LIMIT 10"
  ).all();
  return json2({ runs: results });
}
__name(handleCollectorStatus, "handleCollectorStatus");
async function handleCollectorRun(request, env) {
  const key = request.headers.get("X-Collector-Key");
  if (!env.COLLECTOR_KEY || !key || key !== env.COLLECTOR_KEY) return json2({ error: "unauthorized" }, 401);
  const days = Number(new URL(request.url).searchParams.get("days")) || void 0;
  try {
    const result = await collectCloudflare(env, { days });
    await logRun(env, "manual", "ok", result.saved, result.message);
    return json2({ ok: true, ...result });
  } catch (err) {
    await logRun(env, "manual", "error", 0, err.message);
    return json2({ ok: false, error: err.message }, 500);
  }
}
__name(handleCollectorRun, "handleCollectorRun");
var worker_default = {
  async fetch(request, env) {
    const url = new URL(request.url);
    const { pathname } = url;
    const cors = corsHeaders(request);
    if (request.method === "OPTIONS" && pathname.startsWith("/api/")) {
      return new Response(null, { headers: cors });
    }
    try {
      if (pathname === "/api/tasks") {
        return await handleTasksCollection(request, env, cors);
      }
      const taskMatch = pathname.match(/^\/api\/tasks\/(\d+)$/);
      if (taskMatch) {
        return await handleTaskItem(request, env, cors, taskMatch[1]);
      }
      if (pathname === "/api/ram" && request.method === "GET") {
        return await handleRam(url, env);
      }
      if (pathname === "/api/collector/status" && request.method === "GET") {
        return await handleCollectorStatus(env);
      }
      if (pathname === "/api/collector/run" && request.method === "POST") {
        return await handleCollectorRun(request, env);
      }
      const usageMatch = pathname.match(/^\/api\/usage\/([a-z]+)$/);
      if (usageMatch) {
        return await handleUsage(request, env, usageMatch[1]);
      }
    } catch (err) {
      return json2({ error: err.message }, 500, cors);
    }
    return env.ASSETS.fetch(request);
  },
  // Cron Trigger: Cron läuft nur in UTC. Damit der Lauf ganzjährig um 18:05 und 02:00
  // Zürich-Zeit stattfindet, feuern die Crons zu beiden UTC-Varianten (Sommer-/Winterzeit);
  // hier wird nur der zur aktuellen Zürcher Uhrzeit passende Lauf ausgeführt.
  async scheduled(event, env, ctx) {
    const zurichHour = Number(new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Zurich",
      hour: "2-digit",
      hourCycle: "h23"
    }).format(new Date(event.scheduledTime)));
    if (zurichHour !== 18 && zurichHour !== 2) return;
    ctx.waitUntil((async () => {
      try {
        const result = await collectCloudflare(env);
        console.log("Cloudflare Collector:", JSON.stringify(result));
        await logRun(env, "cron", result.errors ? "partial" : "ok", result.saved, result.message);
      } catch (err) {
        console.error("Cloudflare Collector-Fehler:", err.message);
        await logRun(env, "cron", "error", 0, err.message);
      }
    })());
  }
};

// ../../../tmp/claude-0/-home-claude/5f6a23e9-936a-5aba-a9b2-e319e74be677/scratchpad/wr/node_modules/wrangler/templates/middleware/middleware-ensure-req-body-drained.ts
var drainBody = /* @__PURE__ */ __name(async (request, env, _ctx, middlewareCtx) => {
  try {
    return await middlewareCtx.next(request, env);
  } finally {
    try {
      if (request.body !== null && !request.bodyUsed) {
        const reader = request.body.getReader();
        while (!(await reader.read()).done) {
        }
      }
    } catch (e) {
      console.error("Failed to drain the unused request body.", e);
    }
  }
}, "drainBody");
var middleware_ensure_req_body_drained_default = drainBody;

// ../../../tmp/claude-0/-home-claude/5f6a23e9-936a-5aba-a9b2-e319e74be677/scratchpad/wr/node_modules/wrangler/templates/middleware/middleware-miniflare3-json-error.ts
function reduceError(e) {
  return {
    name: e?.name,
    message: e?.message ?? String(e),
    stack: e?.stack,
    cause: e?.cause === void 0 ? void 0 : reduceError(e.cause)
  };
}
__name(reduceError, "reduceError");
var jsonError = /* @__PURE__ */ __name(async (request, env, _ctx, middlewareCtx) => {
  try {
    return await middlewareCtx.next(request, env);
  } catch (e) {
    const error = reduceError(e);
    const body = JSON.stringify(error);
    const headers = {
      "Content-Type": "application/json",
      "MF-Experimental-Error-Stack": "true"
    };
    const encoded = encodeURIComponent(body);
    if (encoded.length <= 8192) {
      headers["MF-Experimental-Error-Stack-Payload"] = encoded;
    }
    return new Response(body, { status: 500, headers });
  }
}, "jsonError");
var middleware_miniflare3_json_error_default = jsonError;

// .wrangler/tmp/bundle-HiHHeB/middleware-insertion-facade.js
var __INTERNAL_WRANGLER_MIDDLEWARE__ = [
  middleware_ensure_req_body_drained_default,
  middleware_miniflare3_json_error_default
];
var middleware_insertion_facade_default = worker_default;

// ../../../tmp/claude-0/-home-claude/5f6a23e9-936a-5aba-a9b2-e319e74be677/scratchpad/wr/node_modules/wrangler/templates/middleware/common.ts
var __facade_middleware__ = [];
function __facade_register__(...args) {
  __facade_middleware__.push(...args.flat());
}
__name(__facade_register__, "__facade_register__");
function __facade_invokeChain__(request, env, ctx, dispatch, middlewareChain) {
  const [head, ...tail] = middlewareChain;
  const middlewareCtx = {
    dispatch,
    next(newRequest, newEnv) {
      return __facade_invokeChain__(newRequest, newEnv, ctx, dispatch, tail);
    }
  };
  return head(request, env, ctx, middlewareCtx);
}
__name(__facade_invokeChain__, "__facade_invokeChain__");
function __facade_invoke__(request, env, ctx, dispatch, finalMiddleware) {
  return __facade_invokeChain__(request, env, ctx, dispatch, [
    ...__facade_middleware__,
    finalMiddleware
  ]);
}
__name(__facade_invoke__, "__facade_invoke__");

// .wrangler/tmp/bundle-HiHHeB/middleware-loader.entry.ts
var __Facade_ScheduledController__ = class ___Facade_ScheduledController__ {
  constructor(scheduledTime, cron, noRetry) {
    this.scheduledTime = scheduledTime;
    this.cron = cron;
    this.#noRetry = noRetry;
  }
  scheduledTime;
  cron;
  static {
    __name(this, "__Facade_ScheduledController__");
  }
  #noRetry;
  noRetry() {
    if (!(this instanceof ___Facade_ScheduledController__)) {
      throw new TypeError("Illegal invocation");
    }
    this.#noRetry();
  }
};
function wrapExportedHandler(worker) {
  if (__INTERNAL_WRANGLER_MIDDLEWARE__ === void 0 || __INTERNAL_WRANGLER_MIDDLEWARE__.length === 0) {
    return worker;
  }
  for (const middleware of __INTERNAL_WRANGLER_MIDDLEWARE__) {
    __facade_register__(middleware);
  }
  const fetchDispatcher = /* @__PURE__ */ __name(function(request, env, ctx) {
    if (worker.fetch === void 0) {
      throw new Error("Handler does not export a fetch() function.");
    }
    return worker.fetch(request, env, ctx);
  }, "fetchDispatcher");
  return {
    ...worker,
    fetch(request, env, ctx) {
      const dispatcher = /* @__PURE__ */ __name(function(type, init) {
        if (type === "scheduled" && worker.scheduled !== void 0) {
          const controller = new __Facade_ScheduledController__(
            Date.now(),
            init.cron ?? "",
            () => {
            }
          );
          return worker.scheduled(controller, env, ctx);
        }
      }, "dispatcher");
      return __facade_invoke__(request, env, ctx, dispatcher, fetchDispatcher);
    }
  };
}
__name(wrapExportedHandler, "wrapExportedHandler");
function wrapWorkerEntrypoint(klass) {
  if (__INTERNAL_WRANGLER_MIDDLEWARE__ === void 0 || __INTERNAL_WRANGLER_MIDDLEWARE__.length === 0) {
    return klass;
  }
  for (const middleware of __INTERNAL_WRANGLER_MIDDLEWARE__) {
    __facade_register__(middleware);
  }
  return class extends klass {
    #fetchDispatcher = /* @__PURE__ */ __name((request, env, ctx) => {
      this.env = env;
      this.ctx = ctx;
      if (super.fetch === void 0) {
        throw new Error("Entrypoint class does not define a fetch() function.");
      }
      return super.fetch(request);
    }, "#fetchDispatcher");
    #dispatcher = /* @__PURE__ */ __name((type, init) => {
      if (type === "scheduled" && super.scheduled !== void 0) {
        const controller = new __Facade_ScheduledController__(
          Date.now(),
          init.cron ?? "",
          () => {
          }
        );
        return super.scheduled(controller);
      }
    }, "#dispatcher");
    fetch(request) {
      return __facade_invoke__(
        request,
        this.env,
        this.ctx,
        this.#dispatcher,
        this.#fetchDispatcher
      );
    }
  };
}
__name(wrapWorkerEntrypoint, "wrapWorkerEntrypoint");
var WRAPPED_ENTRY;
if (typeof middleware_insertion_facade_default === "object") {
  WRAPPED_ENTRY = wrapExportedHandler(middleware_insertion_facade_default);
} else if (typeof middleware_insertion_facade_default === "function") {
  WRAPPED_ENTRY = wrapWorkerEntrypoint(middleware_insertion_facade_default);
}
var middleware_loader_entry_default = WRAPPED_ENTRY;
export {
  __INTERNAL_WRANGLER_MIDDLEWARE__,
  middleware_loader_entry_default as default
};
//# sourceMappingURL=worker.js.map
