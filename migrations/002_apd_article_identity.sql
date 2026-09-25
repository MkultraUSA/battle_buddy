-- Migration 002: durable APD article identity ledger.
--
-- Adds apd_article_identity, keyed by the stable Google News RSS link
-- (article['link']). The APD news poller's idempotency guard previously keyed
-- on the *resolved* article URL, which is not stable: the source-RSS tier, the
-- Google CSE tier and the Google News /articles/ fallback can each return a
-- different URL on a later cycle, so a retry silently missed its own prior work
-- and inserted a second incident, article link, DM alert, Talk post and seed
-- entry for the same press release.
--
-- Migration-safe: this is one idempotent CREATE TABLE IF NOT EXISTS plus its
-- index. No ALTER, no data rewrite, and not one existing row is touched. It is
-- also applied automatically by the poller itself
-- (APDNewsPoller.ensure_schema) on its next cycle, so a deployment that never
-- runs this file still gains the ledger. The same DDL lives in schema.sql, so a
-- database built fresh from the schema is identical to a migrated one.
--
-- Apply with: sqlite3 calls.db < migrations/002_apd_article_identity.sql

CREATE TABLE IF NOT EXISTS apd_article_identity (
    rss_link     TEXT PRIMARY KEY,
    resolved_url TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT 'apd_pr',
    itype        TEXT NOT NULL DEFAULT '',
    address      TEXT NOT NULL DEFAULT '',
    lat          REAL,
    lon          REAL,
    incident_id  INTEGER,
    state        TEXT NOT NULL DEFAULT 'claimed',
    first_ts     REAL,
    updated_ts   REAL
);

CREATE INDEX IF NOT EXISTS idx_apd_article_identity_incident
    ON apd_article_identity(incident_id)
    WHERE incident_id IS NOT NULL;
