# Graph Report - battle_buddy  (2026-05-11)

## Corpus Check
- 78 files · ~299,926 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1176 nodes · 1611 edges · 86 communities (58 shown, 28 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 180 edges (avg confidence: 0.71)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `23116373`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]
- [[_COMMUNITY_Community 74|Community 74]]
- [[_COMMUNITY_Community 75|Community 75]]
- [[_COMMUNITY_Community 77|Community 77]]
- [[_COMMUNITY_Community 78|Community 78]]
- [[_COMMUNITY_Community 80|Community 80]]
- [[_COMMUNITY_Community 81|Community 81]]
- [[_COMMUNITY_Community 82|Community 82]]
- [[_COMMUNITY_Community 83|Community 83]]
- [[_COMMUNITY_Community 84|Community 84]]
- [[_COMMUNITY_Community 85|Community 85]]

## God Nodes (most connected - your core abstractions)
1. `BasePoller` - 35 edges
2. `APDNewsPoller` - 23 edges
3. `_get_session()` - 22 edges
4. `PiWatchdogService` - 19 edges
5. `TestPollAPDPressReleases` - 18 edges
6. `_make_rss()` - 17 edges
7. `TestClassifyItype` - 17 edges
8. `APDCADPoller` - 16 edges
9. `ADSBAirAssetPoller` - 16 edges
10. `RedditIntelPoller` - 16 edges

## Surprising Connections (you probably didn't know these)
- `test_call()` --calls--> `llm_analyze()`  [INFERRED]
  audio_receiver.py → modules/llm.py
- `test_call()` --calls--> `insert_call()`  [INFERRED]
  audio_receiver.py → modules/database.py
- `test_call()` --calls--> `analyze_for_incident()`  [INFERRED]
  audio_receiver.py → modules/incident_engine.py
- `_BBMetricsCollector` --uses--> `PiWatchdogService`  [INFERRED]
  audio_receiver.py → modules/pi_watchdog.py
- `api_sitrep()` --calls--> `build_sitrep()`  [INFERRED]
  audio_receiver.py → modules/sitrep.py

## Communities (86 total, 28 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.05
Nodes (58): apd_cad_thread(), _apd_fetch_article(), apd_news_thread(), _apd_parse_rss(), _atxfloods_post_to_talk(), atxfloods_thread(), _austin_events_format(), _austin_events_load() (+50 more)

### Community 1 - "Community 1"
Cohesion: 0.05
Nodes (31): _BBMetricsCollector, get_transcription_observability(), _prune_metrics(), Monitors transcription activity and detects hangs.      Tracks the timestamp of, Record that a transcription has started., Record that a transcription completed successfully., Return True if a transcription has been running longer than ``timeout`` seconds., Reset watchdog state (e.g. after restarting the transcription thread). (+23 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (43): api_login(), api_premium_setpassword(), auth_nc_admin(), nginx auth_request endpoint. Returns 200 if caller is a Nextcloud admin, 401 oth, _add_to_talk_rooms(), api_login(), api_logout(), api_me() (+35 more)

### Community 3 - "Community 3"
Cohesion: 0.05
Nodes (36): _banner_api(), _check_commute_alerts(), clear_banner(), create_deck_card(), _point_to_segment_distance_miles(), post_banner(), modules/alerts.py — Banners, Deck cards, and commute alerts.  Moved here from mo, Create a Deck card in the New column when a new incident is detected. (+28 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (26): ADSBAirAssetPoller, alert_leo_aircraft(), alert_orbit(), check_orbit(), ensure_schema(), fetch_aircraft(), _heading_delta(), _km() (+18 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (22): enrich_incident_match(), enrich_tip_location(), ensure_schema(), extract_tip_location(), fetch_feed(), _mark_tip_matched(), _mark_tip_no_data(), _nearby_calls() (+14 more)

### Community 6 - "Community 6"
Cohesion: 0.09
Nodes (10): get_default_service(), pi_watchdog_thread(), PiWatchdogService, modules/pi_watchdog.py ~~~~~~~~~~~~~~~~~~~~~~ Pi / OP25 watchdog service.  Track, Backward-compatible raw-thread entry point., Background service for Pi reachability, OP25, and audio-silence checks., _ImmediateThread, PiWatchdogServiceTests (+2 more)

### Community 7 - "Community 7"
Cohesion: 0.06
Nodes (26): _afd_issue_to_itype(), AFDOpenDataPoller, modules/pollers/impl/afd_news.py ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ AFD Open Data, Map AFD issue_reported string to a Battle Buddy itype., Poll Austin Open Data for active AFD incidents every 60 seconds.      State is h, Fetch AFD incidents and process new/cleared entries., adsb_air_asset_thread(), afd_open_data_thread() (+18 more)

### Community 8 - "Community 8"
Cohesion: 0.09
Nodes (31): api_tgid_confirm(), Manually confirm a TGID name and write it to the tags TSV., analyze_for_incident(), _consider_hold(), _create_incident(), _detect_escalation_stage(), _find_incident_by_location(), _haversine_km() (+23 more)

### Community 9 - "Community 9"
Cohesion: 0.06
Nodes (31): Clone the Repository, code:bash (git clone https://github.com/MkultraUSA/battle_buddy.git), code:ini ([Unit]), code:bash (sudo systemctl daemon-reload), code:bash (journalctl -u battlebuddy.service -f), code:nginx (server {), code:bash (python -m pip install -r requirements-dev.txt), code:bash (cd /opt/battlebuddy) (+23 more)

### Community 10 - "Community 10"
Cohesion: 0.12
Nodes (10): _make_rss(), Network errors during RSS fetch should be caught silently., Build a minimal Google News RSS XML string from a list of item dicts., Integration tests for the APD press-release sub-poll.      All network I/O (urlo, Run _poll_apd_press_releases with mocked network and stubs., Articles with no pubDate should be skipped (no pub_ts)., Articles older than _ARTICLE_MAX_AGE_SECS should be skipped., test_source_rss_tier_resolves_when_title_matches() (+2 more)

### Community 11 - "Community 11"
Cohesion: 0.06
Nodes (30): [0.5.0] — 2026-03-01 (approx.), [0.6.0] — 2026-03-09, [0.7.0] — 2026-03-09, [0.7.1] — 2026-03-09, [0.7.2] — 2026-03-09, [0.7.3] — 2026-03-09, [0.7.6] — 2026-03-09, [0.7.7] — 2026-03-09 (+22 more)

### Community 12 - "Community 12"
Cohesion: 0.11
Nodes (25): api_commute_incidents(), api_commute_polyline(), api_commute_save(), api_commute_share_token(), api_commute_time(), api_intel_query(), api_logout(), api_me() (+17 more)

### Community 13 - "Community 13"
Cohesion: 0.12
Nodes (22): _apd_fetch_article(), _apd_parse_rss(), _append_homicide_json(), _article_itype_from_title(), _match_article_to_incident(), _pi_fetch(), _post_to_talk(), modules/pollers/impl/apd_news.py ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ APD Press Rel (+14 more)

### Community 14 - "Community 14"
Cohesion: 0.19
Nodes (22): cmd_activity(), cmd_aircraft(), cmd_bookings(), cmd_calls(), cmd_events(), cmd_export(), cmd_incidents(), cmd_live() (+14 more)

### Community 15 - "Community 15"
Cohesion: 0.12
Nodes (15): AustinEventsPoller, _days_since(), _format_events(), _load_events(), _load_state(), _post_to_talk(), modules/pollers/impl/austin_events.py ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Aus, Post a weekly Austin major-events digest when the active window changes. (+7 more)

### Community 16 - "Community 16"
Cohesion: 0.1
Nodes (14): BasePoller, APDNewsPoller, Poll Google News RSS for APD press releases every 5 minutes.      State is held, Return a human-readable status string for health checks and tests., ApdNewsPoller, BasePoller, modules/pollers/base.py ~~~~~~~~~~~~~~~~~~~~~~~ Abstract base class for all Batt, Abstract poller that runs run() in a daemon thread every `interval` seconds. (+6 more)

### Community 17 - "Community 17"
Cohesion: 0.12
Nodes (10): APDCADPoller, _parse_cad_ts(), modules/pollers/impl/apd_cad.py ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ Austin PD CAD r, Fetch CAD records from the lookback window and upsert them., Match unmatched CAD rows to scanner incidents and harvest TGID hints., Poll APD CAD records every six hours and enrich matching incidents., Create APD CAD and TGID hint tables if they do not exist., APDCADPollerTests (+2 more)

### Community 18 - "Community 18"
Cohesion: 0.13
Nodes (10): modules/pollers/impl/traffic_open_data.py ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~, Process a new traffic incident. Returns matched incident ID if any., Map traffic issue_reported string to a Battle Buddy itype., Poll Austin Open Data for active traffic incidents every 60 seconds., _traffic_issue_to_itype(), TrafficOpenDataPoller, Unit tests for modules/pollers/impl/traffic_open_data.py., test_matched_minor_incident_posts_without_marker() (+2 more)

### Community 19 - "Community 19"
Cohesion: 0.09
Nodes (9): Unit tests for modules.audio_dedup.  Tests verify thread-safety, TTL eviction, a, Clear module-level state between tests., An entry older than TTL should be evicted and reported as not-duplicate., is_duplicate() must not side-effect the cache., Only one of N concurrent threads registering the same hash wins., _reset(), TestIsDuplicate, TestIsDuplicateAndMark (+1 more)

### Community 20 - "Community 20"
Cohesion: 0.09
Nodes (7): _attach_to_parent(), _load_from_file(), tests/test_apd_poller.py ~~~~~~~~~~~~~~~~~~~~~~~~ Unit test suite for modules/po, # IMPORTANT: we do NOT pre-register any parent package ("modules",, Register a lightweight stub for *name* in sys.modules without touching     any p, Expose a directly loaded/stubbed module on its parent package.      importlib no, _stub_leaf()

### Community 21 - "Community 21"
Cohesion: 0.08
Nodes (23): API Endpoints, Architecture, Battle Buddy, code:text (RTL-SDR / P25 radio source), code:bash (git clone https://github.com/MkultraUSA/battle_buddy.git), code:bash (set -a), code:bash (BATTLE_BUDDY_HOME=/opt/battlebuddy), code:bash (git grep -n -i -E "api[_-]?key|secret|token|password|passwd|) (+15 more)

### Community 22 - "Community 22"
Cohesion: 0.13
Nodes (22): build_classification_rules_text(), _call_openrouter_llm(), _fetch_recommendations(), _get_effective_model(), _is_runtime_banned(), llm_analyze(), llm_identify_tgid(), _notify_tgid_confirmed() (+14 more)

### Community 23 - "Community 23"
Cohesion: 0.14
Nodes (12): ATXFloodsPoller, _post_to_talk(), modules/pollers/impl/atxfloods.py ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ ATXFloods l, Poll ATXFloods and alert on low-water crossing state transitions., Fetch crossings and process status transitions., Process one crossing. Returns True when a transition was handled., _update_marker(), ATXFloodsPollerTests (+4 more)

### Community 24 - "Community 24"
Cohesion: 0.12
Nodes (19): api_calls(), api_flagged_incidents(), api_incidents(), api_incidents_active(), bot_talk(), Verify Nextcloud Talk bot HMAC-SHA256 signature., Return all flagged incidents., Inject a synthetic call for pipeline testing — bypasses Whisper. (+11 more)

### Community 25 - "Community 25"
Cohesion: 0.12
Nodes (20): _atak_clear_marker(), _atak_post_marker(), _atak_resync_on_startup(), _atak_resync_thread(), _atak_send_cot(), _fts_build_ctx(), _fts_connect(), _fts_keepalive_thread() (+12 more)

### Community 26 - "Community 26"
Cohesion: 0.14
Nodes (8): api_sitrep(), api_voice_sitrep(), Returns a clean, natural-language spoken sitrep for TTS., build_sitrep(), build_voice_sitrep(), modules/sitrep.py ~~~~~~~~~~~~~~~~~ Situation report formatters., Unit tests for modules/sitrep.py., SitrepTests

### Community 28 - "Community 28"
Cohesion: 0.14
Nodes (13): code:text ([ ] No real Broadcastify credentials), code:bash (git grep -n -i -E "api[_-]?key|secret|token|password|passwd|), code:bash (git log --all -p | grep -i -E "api[_-]?key|secret|token|pass), code:bash (gitleaks detect --source . --verbose), code:bash (find . -type f \( -name "*.env" -o -name "*config*" -o -name), If a Secret Was Committed, Pre-Release Security Checklist, Public Safety and Privacy (+5 more)

### Community 29 - "Community 29"
Cohesion: 0.18
Nodes (5): Send a breaking incident DM alert to subscribed users., send_dm_alert(), DMAlertTests, _ImmediateThread, Unit tests for DM alert helpers in modules/alerts.py.

### Community 31 - "Community 31"
Cohesion: 0.15
Nodes (6): _make_db(), Verify that the _APD_NEWS_LOCK prevents race conditions when multiple     poller, Create the minimal schema tables required by apd_news., Return a path to a fresh temp DB file (caller responsible for cleanup)., TestConcurrentDedup, _tmp_db()

### Community 32 - "Community 32"
Cohesion: 0.15
Nodes (12): Branch Workflow, Code Style, code:bash (git clone https://github.com/MkultraUSA/battle_buddy.git), code:bash (git checkout -b feature/my-change), code:bash (git grep -n -i -E "api[_-]?key|secret|token|password|passwd|), code:bash (gitleaks detect --source . --verbose), code:text ([ ] The change is focused and documented), Contributing (+4 more)

### Community 33 - "Community 33"
Cohesion: 0.17
Nodes (5): api_homicides(), public_feed_rss(), Battle Buddy public-facing page routes.  Extracted from audio_receiver.py to kee, Return 2026 homicide data for the heat map — static seed + live DB incidents., RSS 2.0 feed of confirmed Battle Buddy incidents (last 200, 30 days).

### Community 35 - "Community 35"
Cohesion: 0.17
Nodes (11): Code Quality, code:text ([ ] README explains the project in under 60 seconds), code:text ([ ] No real Broadcastify credentials), code:text ([ ] Python virtual environment instructions are documented), code:text ([ ] Tests pass), code:text ([ ] The repo makes the maintainer look security-aware), Documentation, Hiring-Manager Readiness (+3 more)

### Community 36 - "Community 36"
Cohesion: 0.27
Nodes (10): correlate_booking_to_incident(), fetch_apd_page(), get_last_occurred_date(), init_db(), main(), poll_apd(), poll_tcso(), Placeholder for TCSO live jail roster.     The SIPS SOAP endpoint (public.travis (+2 more)

### Community 37 - "Community 37"
Cohesion: 0.24
Nodes (10): receive(), _evict_expired(), is_duplicate(), is_duplicate_and_mark(), mark_seen(), audio_dedup.py — Audio deduplication for Battle Buddy.  Tracks SHA-256 hashes of, Remove entries older than _DEDUP_TTL.  Must be called with _seen_lock held., Return True if *audio_hash* was already seen within the TTL window.      Does ** (+2 more)

### Community 39 - "Community 39"
Cohesion: 0.2
Nodes (9): API Exposure and Route Security, Authentication Guidance, code:text (Internet), code:nginx (location /api/incidents {), Nginx Sketch, Public Safety / Privacy Notes, Recommended Exposure Model, Route Classes (+1 more)

### Community 41 - "Community 41"
Cohesion: 0.22
Nodes (8): Architecture, Capture Node, code:text (Radio system / SDR), Data Stores, External Integrations, High-Level Flow, Security Boundaries, Server Node

### Community 43 - "Community 43"
Cohesion: 0.36
Nodes (6): capture_loop(), get_current_tgid(), _journal_follower(), pcm_to_wav(), post_call(), Follow op25-multi_rx journal, parse voice update lines.

### Community 44 - "Community 44"
Cohesion: 0.25
Nodes (7): CI pipeline, code:bash (pip install -r requirements.txt -r requirements-dev.txt), code:bash (pytest --cov=modules --cov-report=term-missing), Running tests locally, Running with coverage, Test files, Testing

### Community 45 - "Community 45"
Cohesion: 0.43
Nodes (6): gh(), push_logs(), Push log lines to Loki. entries = [(labels_dict, message)], Write Prometheus textfile format for Alloy to scrape., run(), write_metrics()

### Community 46 - "Community 46"
Cohesion: 0.52
Nodes (6): build_ffmpeg_cmd(), capture_loop(), main(), pcm_to_wav(), post_call(), rms_db()

### Community 47 - "Community 47"
Cohesion: 0.38
Nodes (6): _find_closest_value(), _get_nws_weather(), _parse_nws_forecast(), Helper to combine NWS 12-hour forecast periods into daily summaries.     This is, Find the value entry in a NWS time series that is closest to the target datetime, Fetch weather from NWS API. Returns a dict of current conditions and a 5-day

### Community 48 - "Community 48"
Cohesion: 0.33
Nodes (4): BaseHTTPRequestHandler, call_groq(), Forward analysis request to Groq, return parsed JSON., RelayHandler

### Community 50 - "Community 50"
Cohesion: 0.53
Nodes (5): init_db(), log_activity(), main(), poll_op25(), POST empty command, return list of active voice channels.

### Community 51 - "Community 51"
Cohesion: 0.4
Nodes (3): APDNewsPoller must be importable from modules.pollers (the package)., APDNewsPoller should appear in the pollers package __all__ (if defined)., TestExportedFromInit

### Community 52 - "Community 52"
Cohesion: 0.6
Nodes (4): _fresh_config_module(), The config module should import with no real deployment secrets present., test_config_imports_without_required_secrets(), test_config_paths_can_be_overridden()

### Community 53 - "Community 53"
Cohesion: 0.4
Nodes (4): [2026-04-27] Refactored APD Press Release Poller, Changes, How to Validate, Rollback

### Community 55 - "Community 55"
Cohesion: 0.5
Nodes (4): _export_incident_snapshot(), _nc_upload(), Upload a file to Nextcloud via WebDAV., Build a markdown snapshot of a flagged incident and push to Nextcloud.

### Community 56 - "Community 56"
Cohesion: 0.83
Nodes (3): load_config(), main(), parse_tgids()

### Community 57 - "Community 57"
Cohesion: 0.5
Nodes (3): Refactor APD Press Release poller (`modules/pollers.py` lines 218-630) into `modules/pollers/impl/apd_news.py` - COMPLETE, TODO, [x] Refactor APD Press Release poller (`modules/pollers.py` lines 218-630) into `modules/pollers/impl/apd_news.py`

## Knowledge Gaps
- **357 isolated node(s):** `Compatibility endpoint for external backlog workers.     Current pipeline is pus`, `Receive Pi watchdog events and forward as Talk DM alerts.`, `Pi polls this endpoint for pending commands (restart_op25, etc.).`, `Inject a synthetic call for pipeline testing — bypasses Whisper.`, `Returns a clean, natural-language spoken sitrep for TTS.` (+352 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **28 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `BasePoller` connect `Community 16` to `Community 34`, `Community 4`, `Community 5`, `Community 38`, `Community 7`, `Community 40`, `Community 42`, `Community 10`, `Community 15`, `Community 17`, `Community 18`, `Community 49`, `Community 51`, `Community 23`, `Community 27`, `Community 30`, `Community 31`?**
  _High betweenness centrality (0.267) - this node is a cross-community bridge._
- **Why does `_nc_create_user()` connect `Community 2` to `Community 5`?**
  _High betweenness centrality (0.117) - this node is a cross-community bridge._
- **Why does `RedditIntelPollerTests` connect `Community 5` to `Community 16`?**
  _High betweenness centrality (0.105) - this node is a cross-community bridge._
- **Are the 29 inferred relationships involving `BasePoller` (e.g. with `ApdNewsPoller` and `APDCADPoller`) actually correct?**
  _`BasePoller` has 29 INFERRED edges - model-reasoned connections that need verification._
- **Are the 15 inferred relationships involving `APDNewsPoller` (e.g. with `BasePoller` and `TestConstants`) actually correct?**
  _`APDNewsPoller` has 15 INFERRED edges - model-reasoned connections that need verification._
- **Are the 18 inferred relationships involving `_get_session()` (e.g. with `api_logout()` and `api_me()`) actually correct?**
  _`_get_session()` has 18 INFERRED edges - model-reasoned connections that need verification._
- **Are the 5 inferred relationships involving `PiWatchdogService` (e.g. with `_BBMetricsCollector` and `_Response`) actually correct?**
  _`PiWatchdogService` has 5 INFERRED edges - model-reasoned connections that need verification._