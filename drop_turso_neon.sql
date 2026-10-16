-- Einmalig im Cloudflare Dashboard (D1 -> db_admin -> Console) ausführen, sobald die
-- Turso-/Neon-Historie nicht mehr gebraucht wird. DROP ist nicht umkehrbar.
-- Vorher optional sichern: SELECT * FROM turso_usage; SELECT * FROM neon_usage;
DROP INDEX IF EXISTS idx_turso_datum;
DROP INDEX IF EXISTS idx_neon_datum;
DROP TABLE IF EXISTS turso_usage;
DROP TABLE IF EXISTS neon_usage;
