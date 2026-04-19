-- pyleans membership schema. Idempotent — safe to run on every silo start.

CREATE TABLE IF NOT EXISTS pyleans_membership (
    cluster_id      TEXT        NOT NULL,
    silo_id         TEXT        NOT NULL,
    host            TEXT        NOT NULL,
    port            INTEGER     NOT NULL,
    epoch           BIGINT      NOT NULL,
    status          TEXT        NOT NULL,
    gateway_port    INTEGER,
    start_time      TIMESTAMPTZ NOT NULL,
    last_heartbeat  TIMESTAMPTZ NOT NULL,
    i_am_alive      TIMESTAMPTZ NOT NULL,
    suspicions      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    etag_version    BIGINT      NOT NULL DEFAULT 0,
    PRIMARY KEY (cluster_id, silo_id)
);

CREATE INDEX IF NOT EXISTS pyleans_membership_status_idx
    ON pyleans_membership (cluster_id, status);

CREATE TABLE IF NOT EXISTS pyleans_membership_version (
    cluster_id TEXT PRIMARY KEY,
    version    BIGINT NOT NULL DEFAULT 0
);
