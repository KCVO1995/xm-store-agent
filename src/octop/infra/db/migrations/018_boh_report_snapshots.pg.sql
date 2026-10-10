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

-- Per-expert chat selectors and per-thread selected context.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS chat_selectors TEXT NOT NULL DEFAULT '[]';
ALTER TABLE threads ADD COLUMN IF NOT EXISTS chat_context_json TEXT NOT NULL DEFAULT '{}';
UPDATE agents SET chat_selectors = '["xm_store"]'
WHERE template_name = 'xm-store-assistant' OR name = '门店助手';

UPDATE _schema_version SET version = 18;
