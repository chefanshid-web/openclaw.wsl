-- OpenClaw Quant Lab: Agent Health Monitor Schema
-- Tracks agent heartbeats, latencies, and critical error states for SRE auto-healing

CREATE TABLE IF NOT EXISTS agent_health (
    agent_name TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'HEALTHY',
    latency_ms DOUBLE PRECISION,
    last_heartbeat TIMESTAMPTZ DEFAULT now(),
    error_msg TEXT
);

-- Index for quick staleness queries by SRE Watchdog
CREATE INDEX IF NOT EXISTS idx_agent_health_last_heartbeat ON agent_health (last_heartbeat DESC);
