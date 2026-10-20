"""
OpenClaw Quant Lab - Cloud Foundation Setup
setup_cloud.py

Uses the Supabase client to configure the cloud database schema:
1. Signals Table & retry_count migration
2. System State (Kill Switch) Table
3. Trade Plans Table
"""

import os
import sys
from dotenv import load_dotenv
from supabase import create_client, Client

SQL_COMMANDS = [
    (
        "Signals Table & Index",
        """
CREATE TABLE IF NOT EXISTS signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    known_at_time TIMESTAMPTZ NOT NULL,
    signal_type TEXT NOT NULL,
    score DOUBLE PRECISION NOT NULL,
    retry_count INT DEFAULT 0,
    metadata JSONB DEFAULT '{}'::jsonb,
    CONSTRAINT chk_known_at_after_event CHECK (known_at_time >= event_time)
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_event_time ON signals (symbol, event_time DESC);
ALTER TABLE signals ADD COLUMN IF NOT EXISTS retry_count INT DEFAULT 0;
        """.strip()
    ),
    (
        "System State (Kill Switch) Table & Default Row",
        """
CREATE TABLE IF NOT EXISTS system_state (
    id INT PRIMARY KEY DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    reason TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);
INSERT INTO system_state (id, status, updated_at) VALUES (1, 'RUNNING', now()) ON CONFLICT (id) DO NOTHING;
        """.strip()
    ),
    (
        "Trade Plans Table & Index",
        """
CREATE TABLE IF NOT EXISTS trade_plans (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id UUID,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    stop_loss DOUBLE PRECISION NOT NULL,
    take_profit DOUBLE PRECISION NOT NULL,
    atr DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    plan_metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trade_plans_status ON trade_plans (status);
        """.strip()
    ),
    (
        "Factory Ledger Table & Indexes",
        """
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
        """.strip()
    ),
]


def setup_cloud():
    print("=" * 65)
    print(" OpenClaw Quant Lab: Cloud Database Setup (setup_cloud.py)")
    print("=" * 65)

    env_path = os.path.join(os.path.dirname(__file__), ".env")
    load_dotenv(env_path)

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")

    if not supabase_url or not supabase_key:
        print("[!] Error: SUPABASE_URL or SUPABASE_KEY is missing in .env file.")
        sys.exit(1)

    print(f"[*] Target Supabase Project: {supabase_url}")

    try:
        client: Client = create_client(supabase_url, supabase_key)
        print("[+] Supabase client initialized successfully.\n")
    except Exception as e:
        print(f"[!] Failed to initialize Supabase client: {e}")
        sys.exit(1)

    print("[*] Attempting raw SQL execution via Supabase RPC ('exec_sql')...\n")

    for name, sql in SQL_COMMANDS:
        print(f"--- Executing: {name} ---")
        try:
            res = client.rpc("exec_sql", {"sql": sql}).execute()
            print(f"[+] Successfully executed: {name}")
        except Exception as e:
            print(f"[!] RPC execution failed: {e}")

    print("\n" + "=" * 65)
    print(" Schema Verification Status")
    print("=" * 65)

    tables = ["signals", "system_state", "trade_plans", "factory_ledger"]
    verified = {}

    for tbl in tables:
        try:
            resp = client.table(tbl).select("*").limit(1).execute()
            verified[tbl] = True
            print(f"[+] Table '{tbl}': ACCESSIBLE (Ready)")
        except Exception as e:
            verified[tbl] = False
            print(f"[-] Table '{tbl}': NOT FOUND or NOT ACCESSIBLE")

    all_ready = all(verified.values())

    if all_ready:
        print("\n" + "=" * 65)
        print(" [SUCCESS] All tables verified and ready for Phase 1 Lab!")
        print("=" * 65)
    else:
        print("\n" + "!" * 65)
        print(" [ACTION REQUIRED] Direct SQL DDL execution via HTTP API is")
        print(" blocked by Supabase security policies unless an RPC function exists.")
        print("")
        print(" To complete setup, please run the SQL script in your Supabase Dashboard:")
        print(" 1. Open: https://supabase.com/dashboard/project/kzwmwyzidelswyvltxsk/sql/new")
        print(" 2. Paste and run the following combined SQL:\n")
        combined_sql = "\n\n".join(sql for _, sql in SQL_COMMANDS)
        print(combined_sql)
        print("\n" + "!" * 65)


if __name__ == "__main__":
    setup_cloud()
