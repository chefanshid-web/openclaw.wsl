-- ==============================================================================
-- OpenClaw Quant Desk - TradingAgents Architecture: Factory Ledger Table
-- File: factory_ledger.sql
--
-- Description:
-- Permanent, append-only database tracking the full lifecycle of every strategy
-- candidate, test, rejection, and mathematical reason for failure/promotion.
-- ==============================================================================

CREATE TABLE IF NOT EXISTS factory_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMPTZ DEFAULT now(),
    strategy_name TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    metrics JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_factory_ledger_timestamp ON factory_ledger (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_factory_ledger_strategy ON factory_ledger (strategy_name);
CREATE INDEX IF NOT EXISTS idx_factory_ledger_action ON factory_ledger (action);

-- Enable Row-Level Security
ALTER TABLE IF EXISTS factory_ledger ENABLE ROW LEVEL SECURITY;

-- Allow full access to service_role
DROP POLICY IF EXISTS "service_role_all_access_factory_ledger" ON factory_ledger;
CREATE POLICY "service_role_all_access_factory_ledger"
ON factory_ledger
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

-- Deny access to anonymous clients
DROP POLICY IF EXISTS "anon_deny_all_factory_ledger" ON factory_ledger;
CREATE POLICY "anon_deny_all_factory_ledger"
ON factory_ledger
FOR ALL
TO anon
USING (false)
WITH CHECK (false);

GRANT ALL ON TABLE factory_ledger TO service_role;
REVOKE ALL ON TABLE factory_ledger FROM anon;
