// src/collectors.js
// Portierung der drei Python-Collector-Skripte nach JS, laufen als
// Cloudflare Cron Triggers im selben Worker wie das Frontend/API.
// Benötigte Secrets (Worker -> Settings -> Variables and Secrets):
//   TURSO_API_TOKEN, TURSO_ORG_SLUG
//   NEON_API_KEY, NEON_ORG_ID (optional)
//   CF_API_TOKEN, CF_ACCOUNT_ID, CF_ZONE_ID (optional)

function pad(n) { return String(n).padStart(2, "0"); }
function fmtDateUTC(d) { return `${pad(d.getUTCDate())}.${pad(d.getUTCMonth() + 1)}.${d.getUTCFullYear()}`; }
function fmtDateISO(d) { return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`; }

async function insertRows(env, table, cols, rows) {
  if (!rows.length) return 0;
  const placeholders = cols.map(() => "?").join(", ");
  const stmt = env.DB.prepare(`INSERT OR IGNORE INTO ${table} (${cols.join(", ")}) VALUES (${placeholders})`);
  let inserted = 0;
  for (const row of rows) {
    const values = cols.map(c => row[c] ?? 0);
    const res = await stmt.bind(...values).run();
    if (res.meta.changes > 0) inserted++;
  }
  return inserted;
}

// ============ Turso ============

function findKeyCI(node, target) {
  const queue = [node];
  while (queue.length) {
    const current = queue.shift();
    if (current && typeof current === "object" && !Array.isArray(current)) {
      for (const [k, v] of Object.entries(current)) {
        if (k.toLowerCase() === target.toLowerCase()) return v;
        queue.push(v);
      }
    } else if (Array.isArray(current)) {
      for (const item of current) queue.push(item);
    }
  }
  return null;
}

function extractFieldsFromDict(d) {
  const wanted = { rows_read: 0, rows_written: 0, bytes_synced: 0, storage_bytes: 0 };
  const lowerKeys = {};
  for (const k of Object.keys(d)) lowerKeys[k.toLowerCase().replace(/_/g, "")] = k;
  for (const field of Object.keys(wanted)) {
    const variant = field.replace(/_/g, "");
    if (lowerKeys[variant] !== undefined) wanted[field] = d[lowerKeys[variant]];
  }
  return wanted;
}

function findUsageFields(node) {
  const wanted = { rows_read: null, rows_written: null, bytes_synced: null, storage_bytes: null };
  let foundAny = false;
  function walk(obj) {
    if (obj && typeof obj === "object" && !Array.isArray(obj)) {
      const lowerKeys = {};
      for (const k of Object.keys(obj)) lowerKeys[k.toLowerCase()] = k;
      let localHit = false;
      for (const field of Object.keys(wanted)) {
        const variant = field.replace(/_/g, "");
        for (const [lk, origK] of Object.entries(lowerKeys)) {
          if (lk.replace(/_/g, "") === variant) {
            wanted[field] = obj[origK];
            localHit = true;
            foundAny = true;
          }
        }
      }
      if (localHit && Object.values(wanted).every(v => v !== null)) return;
      for (const v of Object.values(obj)) walk(v);
    } else if (Array.isArray(obj)) {
      for (const item of obj) walk(item);
    }
  }
  walk(node);
  return foundAny ? wanted : null;
}

async function tursoGetUsage(org, dbName, token, dayStart, dayEnd) {
  const params = new URLSearchParams({
    from: dayStart.toISOString().replace(/\.\d+Z$/, "Z"),
    to: dayEnd.toISOString().replace(/\.\d+Z$/, "Z"),
  });
  const res = await fetch(`https://api.turso.tech/v1/organizations/${org}/databases/${dbName}/usage?${params}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const data = await res.json();

  const totalNode = findKeyCI(data, "total");
  if (totalNode && typeof totalNode === "object") {
    const fields = extractFieldsFromDict(totalNode);
    const hasRowsRead = Object.keys(totalNode).some(k => k.toLowerCase().replace(/_/g, "") === "rowsread");
    if (Object.values(fields).some(v => v !== 0) || hasRowsRead) return fields;
  }
  const fallback = findUsageFields(data);
  if (fallback) return fallback;
  return { rows_read: 0, rows_written: 0, bytes_synced: 0, storage_bytes: 0 };
}

export async function collectTurso(env) {
  const token = env.TURSO_API_TOKEN;
  const org = env.TURSO_ORG_SLUG;
  if (!token || !org) throw new Error("TURSO_API_TOKEN/TURSO_ORG_SLUG fehlen");

  const now = new Date();
  const dayEnd = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  const dayStart = new Date(dayEnd.getTime() - 24 * 3600 * 1000);
  const datumStr = fmtDateUTC(dayStart);

  const listRes = await fetch(`https://api.turso.tech/v1/organizations/${org}/databases`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const listData = await listRes.json();
  const databases = (listData.databases || []).map(db => db.Name);

  const rows = [];
  for (const dbName of databases) {
    try {
      const usage = await tursoGetUsage(org, dbName, token, dayStart, dayEnd);
      rows.push({
        Datum: datumStr,
        Datenbank: dbName,
        RowsRead: usage.rows_read || 0,
        RowsWritten: usage.rows_written || 0,
        BytesSynced: usage.bytes_synced || 0,
        StorageBytes: usage.storage_bytes || 0,
      });
    } catch (e) {
      console.error(`Turso ${dbName} Fehler:`, e.message);
    }
  }
  const inserted = await insertRows(env, "turso_usage",
    ["Datum", "Datenbank", "RowsRead", "RowsWritten", "BytesSynced", "StorageBytes"], rows);
  return { source: "turso", received: rows.length, inserted };
}

// ============ Neon ============

async function neonApiGet(path, token) {
  const res = await fetch(`https://console.neon.tech/api/v2${path}`, {
    headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
  });
  if (!res.ok) throw new Error(`Neon ${path} -> HTTP ${res.status}`);
  return res.json();
}

export async function collectNeon(env) {
  const token = env.NEON_API_KEY;
  const orgId = env.NEON_ORG_ID;
  if (!token) throw new Error("NEON_API_KEY fehlt");

  const todayStr = fmtDateUTC(new Date());
  const params = orgId ? `?limit=100&org_id=${encodeURIComponent(orgId)}` : "?limit=100";
  const listData = await neonApiGet(`/projects${params}`, token);
  const projects = listData.projects || [];

  const rows = [];
  for (const p of projects) {
    const projectId = p.id;
    const projectName = p.name || projectId;
    try {
      const detailData = await neonApiGet(`/projects/${projectId}`, token);
      const detail = detailData.project || {};
      const branchesData = await neonApiGet(`/projects/${projectId}/branches`, token);
      const storageBytes = (branchesData.branches || []).reduce((s, b) => s + (b.logical_size || 0), 0);

      rows.push({
        Datum: todayStr,
        Projekt: projectName,
        ComputeTimeSeconds: detail.compute_time_seconds || 0,
        ActiveTimeSeconds: detail.active_time_seconds || 0,
        WrittenDataBytes: detail.written_data_bytes || 0,
        DataTransferBytes: detail.data_transfer_bytes || 0,
        StorageBytes: storageBytes,
      });
    } catch (e) {
      console.error(`Neon ${projectName} Fehler:`, e.message);
    }
  }
  const inserted = await insertRows(env, "neon_usage",
    ["Datum", "Projekt", "ComputeTimeSeconds", "ActiveTimeSeconds", "WrittenDataBytes", "DataTransferBytes", "StorageBytes"], rows);
  return { source: "neon", received: rows.length, inserted };
}

// ============ Cloudflare ============

const R2_CLASS_A = new Set([
  "PutObject", "CopyObject", "ListObjects", "PutBucket", "CreateMultipartUpload",
  "UploadPart", "CompleteMultipartUpload", "ListMultipartUploads", "ListParts",
  "PutBucketEventNotificationConfig", "LifecycleStorageTiering",
]);

async function cfGraphql(token, query, variables) {
  const res = await fetch("https://api.cloudflare.com/client/v4/graphql", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ query, variables }),
  });
  const data = await res.json();
  if (data.errors && data.errors.length) throw new Error(JSON.stringify(data.errors));
  return data.data;
}

async function safe(label, fn) {
  try { return await fn(); } catch (e) { console.error(`Hinweis: ${label} nicht verfügbar (${e.message})`); return null; }
}

export async function collectCloudflare(env) {
  const token = env.CF_API_TOKEN;
  const accountId = env.CF_ACCOUNT_ID;
  const zoneId = env.CF_ZONE_ID;
  if (!token || !accountId) throw new Error("CF_API_TOKEN/CF_ACCOUNT_ID fehlen");

  const yesterday = new Date(Date.now() - 24 * 3600 * 1000);
  const day = fmtDateISO(yesterday);
  const csvDateStr = fmtDateUTC(yesterday);

  const workers = await safe("Workers", async () => {
    const q = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){workersInvocationsAdaptive(limit:10000,filter:{date:$date}){sum{requests errors}}}}}`;
    const data = await cfGraphql(token, q, { accountTag: accountId, date: day });
    const rows = data.viewer.accounts[0].workersInvocationsAdaptive;
    return [rows.reduce((s, r) => s + r.sum.requests, 0), rows.reduce((s, r) => s + r.sum.errors, 0)];
  }) || [0, 0];

  const kv = await safe("KV", async () => {
    const opsQ = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){kvOperationsAdaptiveGroups(limit:10000,filter:{date:$date}){dimensions{actionType} sum{requests}}}}}`;
    const ops = await cfGraphql(token, opsQ, { accountTag: accountId, date: day });
    const groups = ops.viewer.accounts[0].kvOperationsAdaptiveGroups;
    const reads = groups.filter(g => g.dimensions.actionType === "read").reduce((s, g) => s + g.sum.requests, 0);
    const writes = groups.filter(g => ["write", "delete"].includes(g.dimensions.actionType)).reduce((s, g) => s + g.sum.requests, 0);

    const stQ = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){kvStorageAdaptiveGroups(limit:1,filter:{date:$date},orderBy:[date_DESC]){dimensions{date} max{byteCount}}}}}`;
    const st = await cfGraphql(token, stQ, { accountTag: accountId, date: day });
    const stGroups = st.viewer.accounts[0].kvStorageAdaptiveGroups;
    return [reads, writes, stGroups.length ? stGroups[0].max.byteCount : 0];
  }) || [0, 0, 0];

  const d1 = await safe("D1", async () => {
    const q = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){d1AnalyticsAdaptiveGroups(limit:10000,filter:{date:$date}){sum{readQueries writeQueries rowsRead rowsWritten}}}}}`;
    const data = await cfGraphql(token, q, { accountTag: accountId, date: day });
    const groups = data.viewer.accounts[0].d1AnalyticsAdaptiveGroups;
    const readQ = groups.reduce((s, g) => s + g.sum.readQueries, 0);
    const writeQ = groups.reduce((s, g) => s + g.sum.writeQueries, 0);
    const rowsRead = groups.reduce((s, g) => s + g.sum.rowsRead, 0);
    const rowsWritten = groups.reduce((s, g) => s + g.sum.rowsWritten, 0);

    let storage = 0;
    const dbRes = await fetch(`https://api.cloudflare.com/client/v4/accounts/${accountId}/d1/database`, {
      headers: { Authorization: `Bearer ${token}`, Accept: "application/json" },
    });
    const dbData = await dbRes.json();
    for (const db of dbData.result || []) storage += db.file_size || db.size || 0;

    return [readQ, writeQ, rowsRead, rowsWritten, storage];
  }) || [0, 0, 0, 0, 0];

  const r2 = await safe("R2", async () => {
    const opsQ = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){r2OperationsAdaptiveGroups(limit:10000,filter:{date:$date}){dimensions{actionType} sum{requests}}}}}`;
    const ops = await cfGraphql(token, opsQ, { accountTag: accountId, date: day });
    const groups = ops.viewer.accounts[0].r2OperationsAdaptiveGroups;
    const classA = groups.filter(g => R2_CLASS_A.has(g.dimensions.actionType)).reduce((s, g) => s + g.sum.requests, 0);
    const classB = groups.filter(g => !R2_CLASS_A.has(g.dimensions.actionType)).reduce((s, g) => s + g.sum.requests, 0);

    const stQ = `query($accountTag:string!,$date:string!){viewer{accounts(filter:{accountTag:$accountTag}){r2StorageAdaptiveGroups(limit:1,filter:{date:$date},orderBy:[date_DESC]){dimensions{date} max{payloadSize}}}}}`;
    const st = await cfGraphql(token, stQ, { accountTag: accountId, date: day });
    const stGroups = st.viewer.accounts[0].r2StorageAdaptiveGroups;
    return [classA, classB, stGroups.length ? stGroups[0].max.payloadSize : 0];
  }) || [0, 0, 0];

  let zone = [0, 0];
  if (zoneId) {
    zone = await safe("Zone/CDN", async () => {
      const q = `query($zoneTag:string!,$date:string!){viewer{zones(filter:{zoneTag:$zoneTag}){httpRequests1dGroups(limit:1,filter:{date:$date}){sum{requests bytes}}}}}`;
      const data = await cfGraphql(token, q, { zoneTag: zoneId, date: day });
      const groups = data.viewer.zones[0].httpRequests1dGroups;
      return groups.length ? [groups[0].sum.requests, groups[0].sum.bytes] : [0, 0];
    }) || [0, 0];
  }

  const row = {
    Datum: csvDateStr,
    WorkersRequests: workers[0], WorkersErrors: workers[1],
    KVReads: kv[0], KVWrites: kv[1], KVStorageBytes: kv[2],
    D1ReadQueries: d1[0], D1WriteQueries: d1[1], D1RowsRead: d1[2], D1RowsWritten: d1[3], D1StorageBytes: d1[4],
    R2ClassAOps: r2[0], R2ClassBOps: r2[1], R2StorageBytes: r2[2],
    ZoneRequests: zone[0], ZoneBandwidthBytes: zone[1],
  };
  const cols = ["Datum", "WorkersRequests", "WorkersErrors", "KVReads", "KVWrites", "KVStorageBytes",
    "D1ReadQueries", "D1WriteQueries", "D1RowsRead", "D1RowsWritten", "D1StorageBytes",
    "R2ClassAOps", "R2ClassBOps", "R2StorageBytes", "ZoneRequests", "ZoneBandwidthBytes"];
  const inserted = await insertRows(env, "cloudflare_usage", cols, [row]);
  return { source: "cloudflare", received: 1, inserted };
}
