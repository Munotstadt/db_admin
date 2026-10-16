// src/worker.js
// Einziger Entry-Point: API-Routen (/api/...) werden hier behandelt,
// alles andere fällt durch an die Static Assets (env.ASSETS), die aus dem
// "public/"-Verzeichnis ausgeliefert werden (siehe wrangler.toml).
// Zusätzlich: scheduled() für den täglichen Cloudflare-Usage-Collector (collectors.js).

import { collectCloudflare, logRun, ensureRunsTable, USAGE_COLS } from "./collectors.js";

const ALLOWED_ORIGINS = [
  "https://munotstadt.github.io",
  "https://admin.munot.app",
  "https://energy.munot.app",
  "https://splint.munot.app",
  "https://meteo.munot.app",
  "https://52b.munot.app",
];

function corsHeaders(request) {
  const origin = request.headers.get("Origin");
  const allowOrigin = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];
  return {
    "Access-Control-Allow-Origin": allowOrigin,
    "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Allow-Credentials": "true",
    "Vary": "Origin",
  };
}

function json(data, status, cors) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...(cors || {}) },
  });
}

const USAGE_TABLES = {
  cloudflare: { table: "cloudflare_usage", cols: USAGE_COLS },
};

// Datum liegt als DD.MM.YYYY vor -> für korrekte Sortierung nach ISO umbauen
const ORDER_BY_DATUM = "substr(Datum,7,4) || substr(Datum,4,2) || substr(Datum,1,2)";

async function handleTasksCollection(request, env, cors) {
  if (request.method === "GET") {
    const url = new URL(request.url);
    const status = url.searchParams.get("status");
    const stmt = status
      ? env.DB.prepare("SELECT * FROM improvement_tasks WHERE Status = ? ORDER BY CreatedAt DESC").bind(status)
      : env.DB.prepare("SELECT * FROM improvement_tasks ORDER BY CreatedAt DESC");
    const { results } = await stmt.all();
    return json(results, 200, cors);
  }
  if (request.method === "POST") {
    const body = await request.json();
    const { sourceApp, sourceUrl, title, note, priority } = body;
    if (!sourceApp || !title) return json({ error: "sourceApp und title sind Pflicht" }, 400, cors);
    const result = await env.DB.prepare(
      `INSERT INTO improvement_tasks (SourceApp, SourceUrl, Title, Note, Priority) VALUES (?, ?, ?, ?, ?)`
    ).bind(sourceApp, sourceUrl || null, title, note || null, priority || "normal").run();
    return json({ ok: true, TaskID: result.meta.last_row_id }, 201, cors);
  }
  return json({ error: "method not allowed" }, 405, cors);
}

async function handleTaskItem(request, env, cors, id) {
  if (request.method === "PATCH") {
    const body = await request.json();
    const fields = [];
    const values = [];
    for (const [key, col] of [["title", "Title"], ["note", "Note"], ["status", "Status"], ["priority", "Priority"]]) {
      if (body[key] !== undefined) { fields.push(`${col} = ?`); values.push(body[key]); }
    }
    if (body.status === "done") { fields.push("CompletedAt = ?"); values.push(new Date().toISOString()); }
    fields.push("UpdatedAt = ?"); values.push(new Date().toISOString());
    values.push(id);
    if (fields.length === 1) return json({ error: "nichts zu updaten" }, 400, cors);
    await env.DB.prepare(`UPDATE improvement_tasks SET ${fields.join(", ")} WHERE TaskID = ?`).bind(...values).run();
    return json({ ok: true }, 200, cors);
  }
  if (request.method === "DELETE") {
    await env.DB.prepare("DELETE FROM improvement_tasks WHERE TaskID = ?").bind(id).run();
    return json({ ok: true }, 200, cors);
  }
  return json({ error: "method not allowed" }, 405, cors);
}

async function handleUsage(request, env, source) {
  const cfg = USAGE_TABLES[source];
  if (!cfg) return json({ error: "unbekannte source" }, 404);

  if (request.method === "GET") {
    const { results } = await env.DB.prepare(`SELECT * FROM ${cfg.table} ORDER BY ${ORDER_BY_DATUM} ASC`).all();
    return json(results);
  }
  if (request.method === "POST") {
    const key = request.headers.get("X-Collector-Key");
    if (!key || key !== env.COLLECTOR_KEY) return json({ error: "unauthorized" }, 401);
    const body = await request.json();
    const rows = Array.isArray(body.rows) ? body.rows : [body];
    if (!rows.length) return json({ error: "keine Zeilen übergeben" }, 400);
    const placeholders = cfg.cols.map(() => "?").join(", ");
    const stmt = env.DB.prepare(`INSERT OR IGNORE INTO ${cfg.table} (${cfg.cols.join(", ")}) VALUES (${placeholders})`);
    let inserted = 0;
    for (const row of rows) {
      const values = cfg.cols.map(c => row[c] ?? 0);
      const res = await stmt.bind(...values).run();
      if (res.meta.changes > 0) inserted++;
    }
    return json({ ok: true, inserted, received: rows.length });
  }
  return json({ error: "method not allowed" }, 405);
}

async function handleCollectorStatus(env) {
  await ensureRunsTable(env);
  const { results } = await env.DB.prepare(
    "SELECT RunID, RanAt, TriggerType, Status, DaysCollected, Message FROM collector_runs ORDER BY RunID DESC LIMIT 10"
  ).all();
  return json({ runs: results });
}

// Manueller Lauf: curl -X POST -H "X-Collector-Key: <KEY>" https://admin.munot.app/api/collector/run?days=45
async function handleCollectorRun(request, env) {
  const key = request.headers.get("X-Collector-Key");
  if (!env.COLLECTOR_KEY || !key || key !== env.COLLECTOR_KEY) return json({ error: "unauthorized" }, 401);
  const days = Number(new URL(request.url).searchParams.get("days")) || undefined;
  try {
    const result = await collectCloudflare(env, { days });
    await logRun(env, "manual", "ok", result.saved, result.message);
    return json({ ok: true, ...result });
  } catch (err) {
    await logRun(env, "manual", "error", 0, err.message);
    return json({ ok: false, error: err.message }, 500);
  }
}

export default {
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
      return json({ error: err.message }, 500, cors);
    }

    // Alles andere: Static Assets ausliefern (index.html, tasks.html, assets/...)
    return env.ASSETS.fetch(request);
  },

  // Cron Trigger: Cron läuft nur in UTC. Damit der Lauf ganzjährig um 18:05 und 02:00
  // Zürich-Zeit stattfindet, feuern die Crons zu beiden UTC-Varianten (Sommer-/Winterzeit);
  // hier wird nur der zur aktuellen Zürcher Uhrzeit passende Lauf ausgeführt.
  async scheduled(event, env, ctx) {
    const zurichHour = Number(new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Zurich", hour: "2-digit", hourCycle: "h23",
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
  },
};
