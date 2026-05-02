import sqlite3
from prometheus_client import CollectorRegistry
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from modules.config import DB_PATH

_BB_METRICS_REGISTRY = CollectorRegistry()

class _BBMetricsCollector:
    def collect(self):
        try:
            c = sqlite3.connect(DB_PATH, timeout=5.0)
            cur = c.cursor()

            cur.execute(
                "SELECT COALESCE(itype,'unknown'), COUNT(*) "
                "FROM incidents WHERE (is_test IS NULL OR is_test=0) "
                "GROUP BY itype"
            )
            m = CounterMetricFamily(
                "battlebuddy_incidents",
                "Total non-test incidents detected by Battle Buddy, by itype",
                labels=["itype"],
            )
            for itype, count in cur.fetchall():
                m.add_metric([str(itype)], float(count))
            yield m

            cur.execute("SELECT COUNT(*) FROM calls")
            (call_count,) = cur.fetchone()
            m2 = CounterMetricFamily(
                "battlebuddy_calls",
                "Total transcribed radio calls across all talkgroups",
            )
            m2.add_metric([], float(call_count))
            yield m2

            cur.execute(
                "SELECT "
                "  CASE "
                "    WHEN tag LIKE 'APD%' THEN 'APD' "
                "    WHEN tag LIKE 'AFD%' THEN 'AFD' "
                "    WHEN tag LIKE 'TCEMS%' THEN 'TCEMS' "
                "    WHEN tag LIKE 'TCSO%' THEN 'TCSO' "
                "    WHEN tag LIKE 'LE %' OR tag LIKE 'LE/%' OR tag LIKE 'Lago%' THEN 'LE_other' "
                "    WHEN tag LIKE '%Scanner%' THEN 'scanner_gateway' "
                "    ELSE 'other' "
                "  END AS agency, "
                "  COUNT(*) "
                "FROM calls GROUP BY agency"
            )
            m3 = CounterMetricFamily(
                "battlebuddy_calls_by_agency",
                "Total transcribed radio calls grouped by agency prefix",
                labels=["agency"],
            )
            for agency, count in cur.fetchall():
                m3.add_metric([str(agency)], float(count))
            yield m3

            # --- homicide YTD gauge — sourced from curated homicides_2026.json ---
            try:
                import json as _json
                import os as _os
                _hf = _os.path.join(_os.path.dirname(__file__), "homicides_2026.json")
                _hdata = _json.load(open(_hf))
                _homicide_victims = sum(h.get("count", 1) for h in _hdata)
                _homicide_incidents = len(_hdata)
            except Exception:
                _homicide_victims = 0
                _homicide_incidents = 0
            g_hom_v = GaugeMetricFamily(
                "battlebuddy_homicides_ytd_victims",
                "Austin homicide victims tracked by Battle Buddy, year-to-date 2026",
            )
            g_hom_v.add_metric([], float(_homicide_victims))
            yield g_hom_v
            g_hom_i = GaugeMetricFamily(
                "battlebuddy_homicides_ytd",
                "Austin homicide incidents tracked by Battle Buddy, year-to-date 2026",
            )
            g_hom_i.add_metric([], float(_homicide_incidents))
            yield g_hom_i

            # --- shooting intelligence tiers (30-day window) ---
            import time as _time
            _now = _time.time()
            _30d = _now - (30 * 86400)
            CORROBORATING_AGENCIES = {"AFD", "TCEMS", "TCSO", "TCFD"}

            cur.execute(
                "SELECT agencies FROM incidents "
                "WHERE itype='SHOOTING' AND ts_start >= ? "
                "AND (is_test IS NULL OR is_test=0) "
                "AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%')",
                (_30d,),
            )
            import json as _json2
            _s_confirmed = 0
            _s_signal = 0
            for (_ag,) in cur.fetchall():
                try:
                    _ags = set(_json2.loads(_ag or "[]"))
                except Exception:
                    _ags = set()
                if _ags & CORROBORATING_AGENCIES:
                    _s_confirmed += 1
                elif _ags - {"Unknown", "scanner_gateway", None, ""}:
                    _s_signal += 1

            cur.execute(
                "SELECT COUNT(*) FROM incidents "
                "WHERE itype='SHOOTING' AND ts_start >= ? "
                "AND (is_test IS NULL OR is_test=0) "
                "AND description LIKE '%[APD Press Release]%'",
                (_30d,),
            )
            (_s_press,) = cur.fetchone()

            for _name, _help, _val in [
                ("battlebuddy_shootings_confirmed_30d",
                 "Shooting incidents corroborated by AFD/TCEMS/TCSO radio in last 30 days",
                 _s_confirmed),
                ("battlebuddy_shootings_signal_30d",
                 "Shooting incidents on known agency talkgroup, unverified, last 30 days",
                 _s_signal),
                ("battlebuddy_shootings_press_release_30d",
                 "Shooting incidents from APD press releases in last 30 days",
                 _s_press),
            ]:
                _g = GaugeMetricFamily(_name, _help)
                _g.add_metric([], float(_val))
                yield _g

            # --- live gauges ---
            _window = _now - 86400

            cur.execute(
                "SELECT COUNT(*) FROM incidents "
                "WHERE status='active' AND (is_test IS NULL OR is_test=0) "
                "AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%')"
            )
            (active_count,) = cur.fetchone()
            g_active = GaugeMetricFamily(
                "battlebuddy_active_incidents",
                "Currently active (non-cleared) Battle Buddy incidents",
            )
            g_active.add_metric([], float(active_count))
            yield g_active

            cur.execute(
                "SELECT COALESCE(itype,'unknown'), COUNT(*) FROM incidents "
                "WHERE ts_start >= ? AND (is_test IS NULL OR is_test=0) "
                "AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%') "
                "GROUP BY itype",
                (_window,),
            )
            g24 = GaugeMetricFamily(
                "battlebuddy_incidents_24h",
                "Incidents detected in the last 24 hours by itype",
                labels=["itype"],
            )
            for itype, count in cur.fetchall():
                g24.add_metric([str(itype)], float(count))
            yield g24

            cur.execute(
                "SELECT COUNT(*) FROM calls WHERE ts >= ?",
                (_window,),
            )
            (calls_24h,) = cur.fetchone()
            g_calls = GaugeMetricFamily(
                "battlebuddy_calls_24h",
                "Radio calls received in the last 24 hours",
            )
            g_calls.add_metric([], float(calls_24h))
            yield g_calls

            # --- process memory / thread metrics (leak detection) ---
            try:
                import psutil as _psutil
                _proc = _psutil.Process()
                _mi = _proc.memory_info()
                g_rss = GaugeMetricFamily(
                    'battlebuddy_process_rss_bytes',
                    'Battle Buddy Flask process RSS memory in bytes',
                )
                g_rss.add_metric([], float(_mi.rss))
                yield g_rss
                g_vms = GaugeMetricFamily(
                    'battlebuddy_process_vms_bytes',
                    'Battle Buddy Flask process VMS (virtual) memory in bytes',
                )
                g_vms.add_metric([], float(_mi.vms))
                yield g_vms
                g_thr = GaugeMetricFamily(
                    'battlebuddy_process_threads',
                    'Battle Buddy Flask process thread count',
                )
                g_thr.add_metric([], float(_proc.num_threads()))
                yield g_thr
            except Exception as _pe:
                print(f'[metrics] psutil error: {_pe}', flush=True)

            c.close()
        except Exception as _e:
            print(f"[metrics] collector error: {_e}", flush=True)

_BB_METRICS_REGISTRY.register(_BBMetricsCollector())
