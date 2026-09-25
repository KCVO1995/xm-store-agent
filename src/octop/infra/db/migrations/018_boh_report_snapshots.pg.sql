-- Short-lived, encrypted BOH report pages for deterministic Skill analysis.
CREATE TABLE IF NOT EXISTS boh_report_snapshots (
    snapshot_id TEXT NOT NULL,
    page_index INTEGER NOT NULL,
    user_id BIGINT NOT NULL,
    agent_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    store_id TEXT NOT NULL,
    report_kind TEXT NOT NULL,
    query_json TEXT NOT NULL,
    total INTEGER NOT NULL,
    payload BYTEA NOT NULL,
    expires_at BIGINT NOT NULL,
    PRIMARY KEY (snapshot_id, page_index)
);
CREATE INDEX IF NOT EXISTS idx_boh_report_snapshots_expiry ON boh_report_snapshots(expires_at);
UPDATE _schema_version SET version = 18;
