CREATE TABLE IF NOT EXISTS dbs (
    db_id INTEGER PRIMARY KEY,
    sheurl TEXT NOT NULL,
    dbtoken TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dbs_dbtoken ON dbs (dbtoken);
CREATE INDEX IF NOT EXISTS idx_dbs_sheurl ON dbs (sheurl);
