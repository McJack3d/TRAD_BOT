-- TimescaleDB extension + hypertables for the IBKR sentiment bot.
-- Idempotent: safe to re-run on existing databases.
--
-- The bot creates its own tables via SQLAlchemy on first start; this
-- file ONLY enables the extension and converts the time-series tables
-- to hypertables (cheaper inserts + automatic partitioning).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Hypertable conversions run AFTER the bot has created the base tables.
-- They are wrapped in DO blocks so they no-op if the tables don't exist
-- yet (first boot will skip; subsequent boots convert).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'ibsent_news'
    ) THEN
        PERFORM create_hypertable('ibsent_news', 'published_at',
                                  if_not_exists => TRUE,
                                  migrate_data => TRUE);
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'ibsent_finbert'
    ) THEN
        PERFORM create_hypertable('ibsent_finbert', 'scored_at',
                                  if_not_exists => TRUE,
                                  migrate_data => TRUE);
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'ibsent_llm_verdicts'
    ) THEN
        PERFORM create_hypertable('ibsent_llm_verdicts', 'decided_at',
                                  if_not_exists => TRUE,
                                  migrate_data => TRUE);
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'ibsent_signals'
    ) THEN
        PERFORM create_hypertable('ibsent_signals', 'generated_at',
                                  if_not_exists => TRUE,
                                  migrate_data => TRUE);
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'ibsent_equity'
    ) THEN
        PERFORM create_hypertable('ibsent_equity', 'ts',
                                  if_not_exists => TRUE,
                                  migrate_data => TRUE);
    END IF;
END
$$;
