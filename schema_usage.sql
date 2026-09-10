-- db_admin (D1, id: 78377bf1-f491-4879-bab0-d47a08605f77)
-- Nutzungsdaten-Tabellen (ersetzen data/*.csv)

CREATE TABLE IF NOT EXISTS turso_usage (
  Datum TEXT NOT NULL,          -- DD.MM.YYYY, wie bisher im CSV
  Datenbank TEXT NOT NULL,
  RowsRead INTEGER DEFAULT 0,
  RowsWritten INTEGER DEFAULT 0,
  BytesSynced INTEGER DEFAULT 0,
  StorageBytes INTEGER DEFAULT 0,
  UNIQUE(Datum, Datenbank)
);

CREATE TABLE IF NOT EXISTS neon_usage (
  Datum TEXT NOT NULL,
  Projekt TEXT NOT NULL,
  ComputeTimeSeconds INTEGER DEFAULT 0,
  ActiveTimeSeconds INTEGER DEFAULT 0,
  WrittenDataBytes INTEGER DEFAULT 0,
  DataTransferBytes INTEGER DEFAULT 0,
  StorageBytes INTEGER DEFAULT 0,
  UNIQUE(Datum, Projekt)
);

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

CREATE INDEX IF NOT EXISTS idx_turso_datum ON turso_usage(Datum);
CREATE INDEX IF NOT EXISTS idx_neon_datum ON neon_usage(Datum);
CREATE INDEX IF NOT EXISTS idx_cloudflare_datum ON cloudflare_usage(Datum);
