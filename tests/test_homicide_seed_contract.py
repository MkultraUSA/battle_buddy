"""
tests/test_homicide_seed_contract.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
One seed-path contract for every homicide seed reader, and a canonical read
path that fails visibly instead of reporting zero.

Coverage:
  - resolve_seed_path() / modules.config.resolve_homicide_seed_path() agree
    with the apd_news resolver, and the production default is preserved.
  - apd_news delegates its seed lookup to the shared resolver instead of
    re-implementing the precedence, and empty/whitespace values for every seed
    env var resolve identically in all three modules.
  - empty-string HOMICIDE_SEED_PATH falls back to the data dir.
  - load_seed_strict() raises HomicideSeedUnavailable on missing/corrupt seed.
  - GET /api/homicides answers 503 with an explicit error (never a total of
    zero) and 200 with counts when the seed is healthy.
  - the anonymous 503 body is generic: no absolute seed path, no deployment
    variable, no filename, no parse error — the detail goes to the server log.
  - premium_homicide_summary() reads the same resolved seed and raises rather
    than under-reporting; the /api/premium/homicides/summary route is wired to
    it and no longer hardcodes the production path.
  - No reader touches /opt/battlebuddy: every path opened during the read is
    asserted to live in the test's temporary directory.

No test here reads or writes the production tree: the seed is always a temp
file and the resolved path is asserted to stay inside it.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from modules import homicide_count  # noqa: E402
from modules.homicide_count import (  # noqa: E402
    HomicideSeedUnavailable,
    load_seed_strict,
    premium_homicide_summary,
    resolve_seed_path,
)

_PROD_TREE = "/opt/battlebuddy"
_SEED_BASENAME = "homicides_2026.json"
_SEED_ENV_VARS = ("HOMICIDE_SEED_PATH", "BATTLE_BUDDY_DATA_DIR", "BATTLE_BUDDY_HOME")

_VALID_SEED = [
    {
        "n": 1,
        "date": "2026-01-09",
        "address": "8201 Tuscany Way, Austin, TX",
        "summary": "APD Press Release: Homicide Investigation",
        "url": "https://www.kxan.com/news/apd-press-release-1",
        "lat": 30.4,
        "lon": -97.8,
    },
    {
        "n": 2,
        "date": "2026-03-01",
        "address": "700 W 6th St, Austin, TX",
        "summary": "APD Press Release: Mass shooting",
        "url": "https://www.kvue.com/news/apd-press-release-2",
        "count": 3,
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def seed_env(tmp_path, monkeypatch):
    """Redirect every seed env var into a temp dir holding a valid seed.

    Yields ``(seed_path, data_dir)``. Any path opened by production code must
    stay inside ``data_dir``; the /opt/battlebuddy proof tests assert that.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    seed_path = data_dir / _SEED_BASENAME
    seed_path.write_text(json.dumps(_VALID_SEED), encoding="utf-8")

    for key in _SEED_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BATTLE_BUDDY_HOME", str(data_dir))
    return seed_path, data_dir


@pytest.fixture(autouse=True)
def _real_config_always(real_config):
    """Every test in this file runs against the real ``modules.config``.

    Without this the file's outcome would depend on whether another suite had
    already replaced ``modules.config`` with a partial stub.
    """
    yield real_config


@pytest.fixture
def incidents_db(tmp_path):
    """Minimal incidents table for the live-DB half of the read path."""
    db = tmp_path / "calls.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE incidents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_start    REAL,
            itype       TEXT,
            description TEXT,
            agencies    TEXT,
            location    TEXT,
            lat         REAL,
            lon         REAL,
            article_url TEXT,
            is_test     INTEGER DEFAULT 0
        );
    """)
    conn.execute(
        "INSERT INTO incidents (ts_start, itype, description, agencies, location, "
        "lat, lon, article_url, is_test) VALUES (?,?,?,?,?,?,?,?,0)",
        (1_800_000_000.0, "HOMICIDE", "scanner detection", '["APD"]',
         "500 Congress Ave", 30.26, -97.74, "https://example.com/live-1"),
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def homicide_client(seed_env, incidents_db, monkeypatch):
    """Flask test client for the public blueprint, wired to the temp seed/DB."""
    from flask import Flask

    from modules import public as public_mod

    monkeypatch.setattr(public_mod, "DB_PATH", str(incidents_db))
    app = Flask(__name__)
    app.register_blueprint(public_mod.public_bp)
    app.config.update(TESTING=True)
    return app.test_client()


# ---------------------------------------------------------------------------
# 1. One seed-path contract
# ---------------------------------------------------------------------------

def test_resolve_seed_path_uses_env_overrides(seed_env):
    seed_path, _data_dir = seed_env
    assert resolve_seed_path() == str(seed_path)


def test_config_resolver_and_module_resolver_agree(seed_env, real_config):
    """modules.config is the single source of truth for the precedence."""
    seed_path, _data_dir = seed_env
    assert real_config.resolve_homicide_seed_path() == str(seed_path)
    assert resolve_seed_path() == real_config.resolve_homicide_seed_path()


@contextlib.contextmanager
def _fresh_config_module():
    """Import modules.config from disk, bypassing any sys.modules stub.

    Mirrors tests/test_config.py so HOMICIDE_SEED_PATH is re-snapshotted from
    the environment as it stands right now. The previous entry is always
    restored on exit.
    """
    import importlib

    pkg = sys.modules.get("modules")
    prev = sys.modules.get("modules.config")
    prev_attr = getattr(pkg, "config", None) if pkg is not None else None
    sys.modules.pop("modules.config", None)
    if pkg is not None and hasattr(pkg, "config"):
        delattr(pkg, "config")
    try:
        yield importlib.import_module("modules.config")
    finally:
        if prev is not None:
            sys.modules["modules.config"] = prev
            if pkg is not None and prev_attr is not None:
                pkg.config = prev_attr
        else:
            sys.modules.pop("modules.config", None)


@pytest.fixture
def real_config():
    """Yield the real ``modules.config``, not another suite's stub.

    ``tests/test_apd_cad_poller.py`` registers a minimal ``modules.config``
    stub at import time and never removes it, so the seed contract — which
    lives in the real module — is loaded explicitly here. Whatever was in
    ``sys.modules`` before is restored on teardown.
    """
    with _fresh_config_module() as mod:
        yield mod


def test_config_constant_matches_a_fresh_import(seed_env):
    """HOMICIDE_SEED_PATH is the import-time snapshot of that resolver."""
    with _fresh_config_module() as fresh:
        assert fresh.HOMICIDE_SEED_PATH == str(seed_env[0])
        assert fresh.HOMICIDE_SEED_PATH == fresh.resolve_homicide_seed_path()


@contextlib.contextmanager
def _apd_news_module():
    """Import the apd_news poller module, working around suite-level stubs.

    ``tests/test_apd_cad_poller.py`` registers ``modules.pollers.impl`` as a
    plain module (not a package), which blocks a normal import of
    ``modules.pollers.impl.apd_news``. Fall back to loading the file directly.

    The poller defers its seed lookup to
    ``modules.config.resolve_homicide_seed_path``, so the real
    ``modules.config`` must be importable here. The autouse
    ``_real_config_always`` fixture guarantees that for every test in this file.
    """
    import importlib
    import importlib.util

    try:
        yield importlib.import_module("modules.pollers.impl.apd_news")
        return
    except ImportError:
        pass

    spec = importlib.util.spec_from_file_location(
        "bb_seed_contract_apd_news", _ROOT / "modules" / "pollers" / "impl" / "apd_news.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod


def test_apd_news_resolver_agrees_with_config(seed_env, real_config):
    """The seed writer must resolve the same file the readers do."""
    with _apd_news_module() as apd_news:
        assert apd_news._homicide_seed_path() == str(seed_env[0])
        assert apd_news._homicide_seed_path() == real_config.resolve_homicide_seed_path()


def test_apd_news_resolver_delegates_to_the_shared_contract(seed_env, real_config):
    """apd_news must not carry a second resolver of its own.

    A duplicate resolver is how the two paths drifted before: the poller read
    env with raw truthiness, so an empty or whitespace-only HOMICIDE_SEED_PATH
    meant "unset" to ``modules.config`` but "use the literal whitespace path"
    to the writer.
    """
    with _apd_news_module() as apd_news:
        source = Path(apd_news.__file__).read_text(encoding="utf-8")
        fn = next(
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef) and node.name == "_homicide_seed_path"
        )
        # Drop the docstring: it documents the production default in prose, and
        # the contract under test is what the executable body does.
        body_nodes = fn.body[1:] if ast.get_docstring(fn) else fn.body
        code = ast.unparse(ast.Module(body=body_nodes, type_ignores=[]))
    assert "resolve_homicide_seed_path" in code
    assert "os.environ" not in code, "apd_news must not re-implement the precedence"
    assert _SEED_BASENAME not in code, "apd_news must not hardcode the seed filename"
    assert _PROD_TREE not in code, "apd_news must not hardcode the production tree"


@pytest.mark.parametrize("blank", ["", " ", "\t", "\n", "   \t "])
def test_blank_seed_env_resolves_identically_everywhere(blank, seed_env, real_config,
                                                          monkeypatch):
    """Empty/whitespace env values must mean "unset" for every reader."""
    seed_path, _data_dir = seed_env
    monkeypatch.setenv("HOMICIDE_SEED_PATH", blank)
    monkeypatch.setenv("BATTLE_BUDDY_HOME", str(seed_path.parent))

    assert real_config.resolve_homicide_seed_path() == str(seed_path)
    assert homicide_count.resolve_seed_path() == str(seed_path)
    with _apd_news_module() as apd_news:
        assert apd_news._homicide_seed_path() == str(seed_path)


@pytest.mark.parametrize("blank", ["", " ", "\t", "\n"])
def test_blank_home_and_data_dir_resolve_identically_everywhere(blank, seed_env,
                                                                 real_config, monkeypatch):
    """A blank BATTLE_BUDDY_HOME/DATA_DIR must not resolve to a relative path."""
    seed_path, data_dir = seed_env
    monkeypatch.setenv("HOMICIDE_SEED_PATH", blank)
    monkeypatch.setenv("BATTLE_BUDDY_DATA_DIR", blank)
    monkeypatch.setenv("BATTLE_BUDDY_HOME", blank)

    expected = f"{_PROD_TREE}/{_SEED_BASENAME}"
    assert real_config.resolve_homicide_seed_path() == expected
    assert homicide_count.resolve_seed_path() == expected
    with _apd_news_module() as apd_news:
        assert apd_news._homicide_seed_path() == expected
    # Sanity: the same fixture with a real home still redirects, so the
    # assertions above are about blank handling and not a dead fixture.
    monkeypatch.setenv("BATTLE_BUDDY_HOME", str(data_dir))
    assert real_config.resolve_homicide_seed_path() == str(seed_path)


def test_production_default_unchanged_without_env(monkeypatch, real_config):
    """No env override anywhere keeps /opt/battlebuddy/homicides_2026.json."""
    for key in _SEED_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    assert real_config.resolve_homicide_seed_path() == f"{_PROD_TREE}/{_SEED_BASENAME}"
    assert homicide_count.resolve_seed_path() == f"{_PROD_TREE}/{_SEED_BASENAME}"


def test_data_dir_beats_home(seed_env, monkeypatch, real_config):
    seed_path, data_dir = seed_env
    monkeypatch.setenv("BATTLE_BUDDY_HOME", str(data_dir.parent))
    monkeypatch.setenv("BATTLE_BUDDY_DATA_DIR", str(data_dir))
    assert real_config.resolve_homicide_seed_path() == str(seed_path)


def test_explicit_override_beats_everything(seed_env, monkeypatch, real_config):
    _seed_path, data_dir = seed_env
    custom = data_dir / "custom_seed.json"
    monkeypatch.setenv("HOMICIDE_SEED_PATH", str(custom))
    assert real_config.resolve_homicide_seed_path() == str(custom)


def test_empty_seed_env_falls_back_to_data_dir(seed_env, monkeypatch, real_config):
    """Documented behaviour: an empty HOMICIDE_SEED_PATH is treated as unset."""
    seed_path, _data_dir = seed_env
    monkeypatch.setenv("HOMICIDE_SEED_PATH", "")
    assert real_config.resolve_homicide_seed_path() == str(seed_path)
    assert resolve_seed_path() == str(seed_path)

    monkeypatch.setenv("HOMICIDE_SEED_PATH", "   ")
    assert real_config.resolve_homicide_seed_path() == str(seed_path)


def test_empty_home_falls_back_to_production_default(monkeypatch, real_config):
    for key in _SEED_ENV_VARS:
        monkeypatch.setenv(key, "")
    assert real_config.resolve_homicide_seed_path() == f"{_PROD_TREE}/{_SEED_BASENAME}"


# ---------------------------------------------------------------------------
# 2. Canonical read path fails visibly
# ---------------------------------------------------------------------------

def test_load_seed_strict_returns_entries(seed_env):
    seed_path, _data_dir = seed_env
    assert load_seed_strict() == _VALID_SEED


def test_load_seed_strict_raises_when_missing(seed_env, monkeypatch):
    seed_path, _data_dir = seed_env
    monkeypatch.setenv("HOMICIDE_SEED_PATH", str(seed_path.parent / "absent.json"))
    with pytest.raises(HomicideSeedUnavailable) as exc:
        load_seed_strict()
    assert "absent.json" in str(exc.value)


def test_load_seed_strict_raises_on_corrupt_json(seed_env):
    seed_path, _data_dir = seed_env
    seed_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(HomicideSeedUnavailable):
        load_seed_strict()


def test_load_seed_strict_raises_on_non_list_json(seed_env):
    seed_path, _data_dir = seed_env
    seed_path.write_text(json.dumps({"n": 1}), encoding="utf-8")
    with pytest.raises(HomicideSeedUnavailable):
        load_seed_strict()


def test_tolerant_load_seed_is_still_available_for_non_canonical_callers():
    """The lenient loader keeps its empty-list semantics; it is not the API path."""
    assert homicide_count.load_seed("/nonexistent/homicides_2026.json") == []


# ---------------------------------------------------------------------------
# 3. Public endpoint response behaviour
# ---------------------------------------------------------------------------

def test_api_homicides_returns_counts_with_healthy_seed(homicide_client):
    r = homicide_client.get("/api/homicides")
    assert r.status_code == 200
    body = r.get_json()
    # 2 verified seed entries + 1 live scanner entry.
    assert body["total_area_homicides"] == 3
    assert body["homicides_by_agency"]
    assert len(body["homicides"]) == 3


def test_api_homicides_returns_503_when_seed_missing(homicide_client, seed_env, monkeypatch):
    seed_path, data_dir = seed_env
    monkeypatch.setenv("HOMICIDE_SEED_PATH", str(data_dir / "absent.json"))
    r = homicide_client.get("/api/homicides")
    assert r.status_code == 503
    body = r.get_json()
    assert "homicide seed unavailable" in body["error"]
    # Never a fabricated zero.
    assert "total_area_homicides" not in body
    assert "homicides" not in body


def test_api_homicides_returns_503_when_seed_corrupt(homicide_client, seed_env):
    seed_path, _data_dir = seed_env
    seed_path.write_text("[[[ truncated", encoding="utf-8")
    r = homicide_client.get("/api/homicides")
    assert r.status_code == 503
    assert "homicide seed unavailable" in r.get_json()["error"]


def test_api_homicides_logs_the_fault(homicide_client, seed_env, monkeypatch, caplog):
    seed_path, data_dir = seed_env
    monkeypatch.setenv("HOMICIDE_SEED_PATH", str(data_dir / "absent.json"))
    with caplog.at_level("ERROR", logger="bb.public"):
        homicide_client.get("/api/homicides")
    assert any("seed unavailable" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# 3b. The unauthenticated 503 body discloses nothing about the deployment
# ---------------------------------------------------------------------------

def test_api_homicides_503_body_discloses_no_seed_path(homicide_client, seed_env, monkeypatch):
    """/api/homicides is anonymous: no path, no deployment variable, no detail."""
    seed_path, data_dir = seed_env
    absent = data_dir / "absent.json"
    monkeypatch.setenv("HOMICIDE_SEED_PATH", str(absent))

    r = homicide_client.get("/api/homicides")
    assert r.status_code == 503
    raw = r.get_data(as_text=True)
    body = r.get_json()

    assert body == {"error": "homicide seed unavailable"}
    assert "detail" not in body
    for leak in (
        str(absent),            # resolved absolute seed path
        str(data_dir),          # deployment data dir
        _PROD_TREE,             # production tree
        "HOMICIDE_SEED_PATH",   # deployment variable
        "BATTLE_BUDDY_HOME",
        "BATTLE_BUDDY_DATA_DIR",
        _SEED_BASENAME,         # the curated filename
    ):
        assert leak not in raw, f"503 body leaked {leak!r}"


def test_api_homicides_503_body_is_generic_for_a_corrupt_seed(homicide_client, seed_env):
    """A corrupt seed must not echo the parse error or the path either."""
    seed_path, _data_dir = seed_env
    seed_path.write_text("[[[ truncated", encoding="utf-8")

    r = homicide_client.get("/api/homicides")
    assert r.status_code == 503
    raw = r.get_data(as_text=True)
    assert r.get_json() == {"error": "homicide seed unavailable"}
    assert str(seed_path) not in raw
    assert _SEED_BASENAME not in raw
    assert "Expecting" not in raw and "JSONDecodeError" not in raw


def test_api_homicides_503_detail_is_logged_server_side(homicide_client, seed_env,
                                                         monkeypatch, caplog):
    """The diagnostic detail moves to the log, so operators keep it."""
    seed_path, data_dir = seed_env
    absent = data_dir / "absent.json"
    monkeypatch.setenv("HOMICIDE_SEED_PATH", str(absent))
    with caplog.at_level("ERROR", logger="bb.public"):
        homicide_client.get("/api/homicides")
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "seed unavailable" in logged
    assert str(absent) in logged, "the log must keep the resolved path for operators"


# ---------------------------------------------------------------------------
# 4. Premium summary reads the same seed
# ---------------------------------------------------------------------------

def test_premium_summary_uses_resolved_seed(seed_env, incidents_db):
    payload = premium_homicide_summary(str(incidents_db))
    # seed count 1 + 3 (count field) plus 1 live geocoded homicide
    assert payload["ytd"] == 5
    assert payload["year"] == 2026
    assert payload["last"]["location"] == "500 Congress Ave"


def test_premium_summary_raises_instead_of_reporting_zero(seed_env, incidents_db, monkeypatch):
    seed_path, data_dir = seed_env
    monkeypatch.setenv("HOMICIDE_SEED_PATH", str(data_dir / "absent.json"))
    with pytest.raises(HomicideSeedUnavailable):
        premium_homicide_summary(str(incidents_db))


def test_premium_summary_falls_back_to_newest_seed_entry(seed_env, incidents_db):
    conn = sqlite3.connect(incidents_db)
    conn.execute("DELETE FROM incidents")
    conn.commit()
    conn.close()
    payload = premium_homicide_summary(str(incidents_db))
    assert payload["ytd"] == 4
    assert payload["last"]["location"] == "700 W 6th St, Austin, TX"


def _premium_route_source() -> str:
    """Return the source of the /api/premium/homicides/summary route.

    audio_receiver.py cannot be imported in a test (it builds the whole Flask
    app, installs an unverified TLS opener and pulls optional providers), so
    the route is asserted statically: it must delegate to the shared helper
    instead of opening a seed path of its own.
    """
    src = (_ROOT / "audio_receiver.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "api_premium_homicides_summary":
            return ast.get_source_segment(src, node)
    raise AssertionError("api_premium_homicides_summary not found in audio_receiver.py")


def test_premium_route_delegates_to_shared_seed_contract():
    body = _premium_route_source()
    assert "premium_homicide_summary" in body
    assert "HomicideSeedUnavailable" in body
    assert "503" in body
    assert _PROD_TREE not in body
    assert "homicides_2026.json" not in body


def test_public_module_has_no_hardcoded_production_seed():
    src = (_ROOT / "modules" / "public.py").read_text(encoding="utf-8")
    assert _PROD_TREE not in src
    assert "load_seed_strict" in src


# ---------------------------------------------------------------------------
# 5. Zero access under /opt/battlebuddy
# ---------------------------------------------------------------------------

def test_api_homicides_never_touches_the_production_tree(homicide_client, seed_env, monkeypatch):
    """Every filesystem path touched by the read must be inside the temp dir."""
    seed_path, _data_dir = seed_env
    opened: list[str] = []
    real_open = builtins.open
    real_exists = os.path.exists

    def _recording_open(file, *a, **kw):
        if isinstance(file, (str, bytes, os.PathLike)):
            opened.append(os.fspath(file) if not isinstance(file, bytes) else file.decode())
        return real_open(file, *a, **kw)

    def _recording_exists(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            opened.append(os.fspath(path) if not isinstance(path, bytes) else path.decode())
        return real_exists(path)

    monkeypatch.setattr(builtins, "open", _recording_open)
    monkeypatch.setattr(os.path, "exists", _recording_exists)

    r = homicide_client.get("/api/homicides")
    assert r.status_code == 200

    prod_hits = [p for p in opened if p.startswith(_PROD_TREE)]
    assert not prod_hits, f"production tree accessed: {prod_hits}"
    # The temp seed really was the file that got read.
    assert any(os.path.realpath(p) == os.path.realpath(str(seed_path)) for p in opened)


def test_premium_summary_never_touches_the_production_tree(seed_env, incidents_db, monkeypatch):
    _seed_path, data_dir = seed_env
    opened: list[str] = []
    real_open = builtins.open
    real_exists = os.path.exists

    def _recording_open(file, *a, **kw):
        if isinstance(file, (str, bytes, os.PathLike)):
            opened.append(os.fspath(file) if not isinstance(file, bytes) else file.decode())
        return real_open(file, *a, **kw)

    def _recording_exists(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            opened.append(os.fspath(path) if not isinstance(path, bytes) else path.decode())
        return real_exists(path)

    monkeypatch.setattr(builtins, "open", _recording_open)
    monkeypatch.setattr(os.path, "exists", _recording_exists)

    payload = premium_homicide_summary(str(incidents_db))
    assert payload["ytd"] == 5

    prod_hits = [p for p in opened if p.startswith(_PROD_TREE)]
    assert not prod_hits, f"production tree accessed: {prod_hits}"
    assert any(os.path.realpath(p).startswith(os.path.realpath(data_dir)) for p in opened)
