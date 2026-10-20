-- ==============================================================================
-- OpenClaw Quant Desk - Supabase Row-Level Security (RLS) Policy Script
-- File: supabase_rls.sql
--
-- Purpose:
-- 1. Enables Row-Level Security (RLS) on 'signals', 'trade_plans', and 'system_state'.
-- 2. Grants full READ (SELECT), INSERT, and UPDATE permissions ONLY to 'service_role'.
-- 3. Explicitly denies all access to the 'anon' role (public / unauthenticated clients).
--
-- Instructions:
-- Run this script in the Supabase Dashboard -> SQL Editor.
-- ==============================================================================

-- ------------------------------------------------------------------------------
-- Step 1: Enable Row-Level Security (RLS)
-- ------------------------------------------------------------------------------
ALTER TABLE IF EXISTS signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS trade_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE IF EXISTS system_state ENABLE ROW LEVEL SECURITY;

-- ------------------------------------------------------------------------------
-- Step 2: Clean up previous policies if they exist (Idempotency)
-- ------------------------------------------------------------------------------
-- signals
DROP POLICY IF EXISTS "service_role_all_access_signals" ON signals;
DROP POLICY IF EXISTS "service_role_full_access_signals" ON signals;
DROP POLICY IF EXISTS "anon_deny_all_signals" ON signals;

-- trade_plans
DROP POLICY IF EXISTS "service_role_all_access_trade_plans" ON trade_plans;
DROP POLICY IF EXISTS "service_role_full_access_trade_plans" ON trade_plans;
DROP POLICY IF EXISTS "anon_deny_all_trade_plans" ON trade_plans;

-- system_state
DROP POLICY IF EXISTS "service_role_all_access_system_state" ON system_state;
DROP POLICY IF EXISTS "service_role_full_access_system_state" ON system_state;
DROP POLICY IF EXISTS "anon_deny_all_system_state" ON system_state;

-- ------------------------------------------------------------------------------
-- Step 3: Create Service Role Policies (ALLOW read, insert, update, delete)
-- ------------------------------------------------------------------------------
-- Our bot executes with the SUPABASE_SERVICE_KEY (service_role), granting full control.
CREATE POLICY "service_role_all_access_signals"
ON signals
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

CREATE POLICY "service_role_all_access_trade_plans"
ON trade_plans
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

CREATE POLICY "service_role_all_access_system_state"
ON system_state
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

-- ------------------------------------------------------------------------------
-- Step 4: Create Anon Role Policies (EXPLICIT DENY for public/anon keys)
-- ------------------------------------------------------------------------------
CREATE POLICY "anon_deny_all_signals"
ON signals
FOR ALL
TO anon
USING (false)
WITH CHECK (false);

CREATE POLICY "anon_deny_all_trade_plans"
ON trade_plans
FOR ALL
TO anon
USING (false)
WITH CHECK (false);

CREATE POLICY "anon_deny_all_system_state"
ON system_state
FOR ALL
TO anon
USING (false)
WITH CHECK (false);

-- ------------------------------------------------------------------------------
-- Step 5: Table-Level Privilege Hardening
-- ------------------------------------------------------------------------------
-- Revoke all table-level privileges from anon role
REVOKE ALL ON TABLE signals FROM anon;
REVOKE ALL ON TABLE trade_plans FROM anon;
REVOKE ALL ON TABLE system_state FROM anon;

-- Ensure service_role has table-level access
GRANT ALL ON TABLE signals TO service_role;
GRANT ALL ON TABLE trade_plans TO service_role;
GRANT ALL ON TABLE system_state TO service_role;

-- ------------------------------------------------------------------------------
-- Verification Query: Check that rowsecurity is enabled
-- ------------------------------------------------------------------------------
-- SELECT tablename, rowsecurity 
-- FROM pg_tables 
-- WHERE schemaname = 'public' 
--   AND tablename IN ('signals', 'trade_plans', 'system_state');

-- ------------------------------------------------------------------------------
-- Step 6: Factory Ledger Table Policies
-- ------------------------------------------------------------------------------
ALTER TABLE IF EXISTS factory_ledger ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "service_role_all_access_factory_ledger" ON factory_ledger;
CREATE POLICY "service_role_all_access_factory_ledger" ON factory_ledger FOR ALL TO service_role USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS "anon_deny_all_factory_ledger" ON factory_ledger;
CREATE POLICY "anon_deny_all_factory_ledger" ON factory_ledger FOR ALL TO anon USING (false) WITH CHECK (false);
REVOKE ALL ON TABLE factory_ledger FROM anon;
GRANT ALL ON TABLE factory_ledger TO service_role;
