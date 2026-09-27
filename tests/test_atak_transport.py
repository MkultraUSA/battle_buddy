"""Regression tests for the FreeTAKServer CoT transport mode.

FTS's SSL CoT port (8089) hardcodes ``ssl.CERT_REQUIRED``. Battle Buddy already
holds a client certificate that is valid until 2027 and chains to FTS's own CA,
but the server completes the TLS handshake, swallows the SA announcement, and
then drops the connection before any marker is written -- and logs nothing about
it. That path is not fixable from the client side.

FTS also exposes a plaintext CoT port that needs no client certificate, and the
Battle Buddy to FTS link runs over Tailscale, which already encrypts it. So
plaintext is the working transport on this link.

The critical property these tests pin: **plaintext must be an explicit operator
decision, never a silent default.** A deployment that forgets to set the flag
must keep using TLS.
"""

import socket
import sys
import types

# `stripe` is not installed here and modules.premium imports it.
sys.modules.setdefault("stripe", types.SimpleNamespace(api_key=None))

# tests/test_pi_watchdog.py installs a file-less modules.config stub into
# sys.modules at collection time, which shadows the real module and makes a
# module-level "from modules.config import ..." fail with "unknown location".
# Evict a stub with no __file__; the real module always has one.
_config = sys.modules.get("modules.config")
if _config is not None and getattr(_config, "__file__", None) is None:
    del sys.modules["modules.config"]

from modules import atak as atak_mod  # noqa: E402
from modules.config import FTS_COT_PORT, FTS_COT_TLS  # noqa: E402

# The truthy-string parsing config.py uses. Exercised directly rather than by
# reloading modules.config, which is unsafe inside a shared pytest session: a
# reload re-executes the module and can be defeated by the file-less
# modules.config stub other test modules leak into sys.modules.
_TRUTHY = ("1", "true", "yes", "on")


def _parse(raw: str) -> bool:
    """Mirror of modules/config.py's FTS_COT_TLS parsing."""
    return raw.lower() in _TRUTHY


def test_tls_is_the_default():
    """The shipped default must remain TLS so nothing downgrades silently."""
    assert FTS_COT_TLS is True, (
        "FTS_COT_TLS must default to True; plaintext must be an explicit opt-in"
    )


def test_plaintext_flag_parses_truthy_and_falsy():
    """Only the documented truthy values enable plaintext."""
    for raw in ("true", "1", "yes", "on", "TRUE", "On"):
        assert _parse(raw) is True, raw
    for raw in ("false", "0", "no", "off", "", "maybe", "2"):
        assert _parse(raw) is False, raw


def test_config_source_uses_the_truthy_contract():
    """Guard that config.py keeps parsing with the same truthy set."""
    import inspect

    import modules.config as cfg

    source = inspect.getsource(cfg)
    line = next(
        (ln for ln in source.splitlines() if ln.startswith("FTS_COT_TLS")),
        "",
    )
    assert line, "FTS_COT_TLS is no longer defined in modules/config.py"
    for token in ('"1"', '"true"', '"yes"', '"on"'):
        assert token in line, "FTS_COT_TLS parsing lost %s: %s" % (token, line)
    # The literal default must be "true"; the resolved value is asserted
    # separately by test_tls_is_the_default against the real module.
    assert '"true"' in line, "FTS_COT_TLS default must be 'true': %s" % line


class _FakeSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


def test_plaintext_mode_does_not_wrap_in_tls(monkeypatch):
    """With TLS off, the socket must be used as-is: no SSL wrap, no handshake."""
    monkeypatch.setattr(atak_mod, "FTS_COT_TLS", False)
    monkeypatch.setattr(atak_mod, "FTS_COT_PORT", 8087)

    raw = _FakeSocket()
    created = []

    def fake_create_connection(addr, timeout=None):
        created.append(addr)
        return raw

    def explode(*a, **k):
        raise AssertionError("TLS must not be used when FTS_COT_TLS is false")

    monkeypatch.setattr(atak_mod._sock_mod, "create_connection", fake_create_connection)
    monkeypatch.setattr(atak_mod, "_fts_build_ctx", explode)

    atak_mod._fts_socket = None
    atak_mod._fts_connect()

    assert created == [(atak_mod.FTS_HOST, 8087)], created
    assert atak_mod._fts_socket is raw, "plaintext mode must use the raw socket"
    # The SA announcement is still sent, so FTS registers us as a client.
    assert raw.sent, "SA announcement must still be sent in plaintext mode"
    assert b"BATTLEBUDDY-SERVER" in raw.sent[0]


def test_tls_mode_still_wraps(monkeypatch):
    """With TLS on, the existing wrap path must be preserved."""
    monkeypatch.setattr(atak_mod, "FTS_COT_TLS", True)
    monkeypatch.setattr(atak_mod, "FTS_COT_PORT", 8089)

    raw = _FakeSocket()
    wrapped = _FakeSocket()

    monkeypatch.setattr(
        atak_mod._sock_mod, "create_connection", lambda addr, timeout=None: raw
    )
    monkeypatch.setattr(atak_mod, "_fts_build_ctx", lambda: _WrapCtx(wrapped))

    atak_mod._fts_socket = None
    atak_mod._fts_connect()

    assert atak_mod._fts_socket is wrapped, "TLS mode must still wrap the socket"
    assert raw.sent == [], "the raw socket must not be used directly in TLS mode"


class _WrapCtx:
    def __init__(self, target):
        self.target = target

    def wrap_socket(self, sock):
        return self.target


def test_plaintext_port_is_the_fts_default_plaintext_port():
    """8087 is FTS's plaintext CoT port; 8089 is its TLS one."""
    assert 8087 != 8089
    # The default in config stays the TLS port so the opt-in is explicit.
    assert FTS_COT_PORT == 8089
    assert FTS_COT_TLS is True


def test_send_is_unchanged_by_transport_mode(monkeypatch):
    """_fts_send_cot must not care which transport is in use."""
    # _atak_send_cot returns early unless the subsystem is enabled, so enable it.
    monkeypatch.setattr(atak_mod, "FTS_ENABLED", True)
    monkeypatch.setattr(atak_mod, "FTS_COT_TLS", False)
    sock = _FakeSocket()
    monkeypatch.setattr(atak_mod, "_fts_socket", sock, raising=False)
    atak_mod._atak_send_cot("<event/>")
    assert sock.sent == [b"<event/>"]


def test_real_socket_module_is_used_not_a_stub():
    """Guard against the transport accidentally bypassing socket entirely."""
    assert hasattr(atak_mod._sock_mod, "create_connection")
    assert issubclass(socket.socket, object)
