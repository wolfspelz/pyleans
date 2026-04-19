-- pyleans grain-state schema. Idempotent — safe to run on every silo start.

CREATE TABLE IF NOT EXISTS pyleans_grain_state (
    grain_type  TEXT        NOT NULL,
    grain_key   TEXT        NOT NULL,
    state       JSONB       NOT NULL,
    etag        BIGINT      NOT NULL DEFAULT 1,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (grain_type, grain_key)
);
