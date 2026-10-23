// NAS RAM Monitor: Lese-API für ram.html (Daten schreibt der Worker nas-ram-monitor per Ingest)
// Tabellen in D1 db_admin: nas_ram_monitor (Rohdaten), nas_ram_monitor_hourly (Stundenwerte)
const DAY = 864e5;
const RANGES = {
  1: { ms: 1 * DAY, raw: true },    // 24 h aus den Rohdaten (5-Minuten-Raster)
  7: { ms: 7 * DAY, raw: false },   // ab hier aus den Stundenwerten
  30: { ms: 30 * DAY, raw: false },
  0: { ms: null, raw: false },      // alles
};

const iso = (t) => new Date(t).toISOString().replace(/\.\d{3}Z$/, 'Z');
const r2 = (v) => (v == null ? null : Math.round(v * 100) / 100);
const lo = (a, b) => (a == null ? b : b == null ? a : Math.min(a, b));
const hi = (a, b) => (a == null ? b : b == null ? a : Math.max(a, b));
const json = (obj, status = 200, extra = {}) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', ...extra },
  });


export async function handleRam(url, env) {
  const q = url.searchParams.get('days');
  const days = q !== null && Object.hasOwn(RANGES, q) ? Number(q) : 7;
  const cfg = RANGES[days];
  const since = cfg.ms ? iso(Date.now() - cfg.ms) : null;

  // Einheitliche Struktur: je Zeitfenster (Rohmessung oder Stunde) und Quelle eine Zeile
  let rows;
  if (cfg.raw) {
    const r = await env.DB.prepare(
      'SELECT ts, source, used_mb, total_mb, avail_mb, swap_mb, mem_pct FROM nas_ram_monitor WHERE ts >= ?1 ORDER BY ts',
    ).bind(since).all();
    rows = r.results.map((x) => ({
      ts: x.ts, source: x.source, n: 1, sum_used: x.used_mb, avg_used: x.used_mb, max_used: x.used_mb,
      min_avail: x.avail_mb, max_swap: x.swap_mb, total_mb: x.total_mb, pct: x.mem_pct,
      last_ts: x.ts, last_used: x.used_mb, last_avail: x.avail_mb, last_swap: x.swap_mb,
    }));
  } else {
    const sql = 'SELECT hour, source, n, sum_used, max_used, min_avail, max_swap, total_mb, sum_pct, last_ts, last_used, last_avail, last_swap '
      + 'FROM nas_ram_monitor_hourly' + (since ? ' WHERE hour >= ?1' : '') + ' ORDER BY hour';
    const stmt = env.DB.prepare(sql);
    const r = await (since ? stmt.bind(since.slice(0, 13) + ':00:00Z') : stmt).all();
    rows = r.results.map((x) => ({
      ts: x.hour, source: x.source, n: x.n, sum_used: x.sum_used, avg_used: x.sum_used / x.n, max_used: x.max_used,
      min_avail: x.min_avail, max_swap: x.max_swap, total_mb: x.total_mb,
      pct: x.sum_pct == null ? null : x.sum_pct / x.n,
      last_ts: x.last_ts, last_used: x.last_used, last_avail: x.last_avail, last_swap: x.last_swap,
    }));
  }

  const sys = { min_avail: null, max_swap: null, total: null, last_used: null, last_avail: null, last_swap: null };
  const acc = new Map();
  let lastTs = null, runs = 0, all = 0;
  for (const x of rows) {
    all += x.n;
    if (x.last_ts && (!lastTs || x.last_ts > lastTs)) lastTs = x.last_ts;
    if (x.source === 'system') {
      runs += x.n;
      sys.min_avail = lo(sys.min_avail, x.min_avail);
      sys.max_swap = hi(sys.max_swap, x.max_swap);
      sys.total = x.total_mb ?? sys.total;
      sys.last_used = x.last_used; sys.last_avail = x.last_avail; sys.last_swap = x.last_swap;   // aufsteigend: zuletzt = aktuell
    } else {
      const a = acc.get(x.source) || { source: x.source, sum: 0, n: 0, max: 0, last: null };
      a.sum += x.sum_used; a.n += x.n; a.max = Math.max(a.max, x.max_used); a.last = x.last_used;
      acc.set(x.source, a);
    }
  }
  const stats = [...acc.values()]
    .map((a) => ({ source: a.source, avg_mb: r2(a.sum / a.n), max_mb: r2(a.max), last_mb: r2(a.last) }))
    .sort((a, b) => b.avg_mb - a.avg_mb);
  for (const k of Object.keys(sys)) sys[k] = r2(sys[k]);

  return json({
    days,
    bucket: cfg.raw ? '5 Min (Rohdaten)' : '1 Stunde',
    rows: rows.map((x) => ({
      ts: x.ts, source: x.source, used_mb: r2(x.avg_used), total_mb: r2(x.total_mb),
      avail_mb: r2(x.min_avail), swap_mb: r2(x.max_swap), mem_pct: r2(x.pct),
    })),
    stats,
    system: sys,
    span: { first: rows.length ? rows[0].ts : null, last: lastTs, n: runs || all },
  });
}
