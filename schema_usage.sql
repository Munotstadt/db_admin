-- db_admin (D1, id: 78377bf1-f491-4879-bab0-d47a08605f77)
-- Cloudflare-Nutzungsdaten (Turso/Neon entfernt, siehe drop_turso_neon.sql)

CREATE TABLE IF NOT EXISTS cloudflare_usage (
  Datum TEXT NOT NULL UNIQUE,
  WorkersRequests INTEGER DEFAULT 0,
  WorkersErrors INTEGER DEFAULT 0,
  KVReads INTEGER DEFAULT 0,
  KVWrites INTEGER DEFAULT 0,
  KVStorageBytes INTEGER DEFAULT 0,
  D1ReadQueries INTEGER DEFAULT 0,
  D1WriteQueries INTEGER DEFAULT 0,
  D1RowsRead INTEGER DEFAULT 0,
  D1RowsWritten INTEGER DEFAULT 0,
  D1StorageBytes INTEGER DEFAULT 0,
  R2ClassAOps INTEGER DEFAULT 0,
  R2ClassBOps INTEGER DEFAULT 0,
  R2StorageBytes INTEGER DEFAULT 0,
  ZoneRequests INTEGER DEFAULT 0,
  ZoneBandwidthBytes INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cloudflare_datum ON cloudflare_usage(Datum);

-- Lauf-Log des Cron-Collectors (wird vom Worker bei Bedarf auch selbst angelegt)
CREATE TABLE IF NOT EXISTS collector_runs (
  RunID INTEGER PRIMARY KEY AUTOINCREMENT,
  RanAt TEXT NOT NULL DEFAULT (datetime('now')),
  TriggerType TEXT,
  Status TEXT,
  DaysCollected INTEGER DEFAULT 0,
  Message TEXT
);
