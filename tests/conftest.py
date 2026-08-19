from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from mock_superset import RECEIVED, VALID_PASSWORD, VALID_USER, serve  # noqa: E402

CONFIG_TEMPLATE = """
[instance]
base_url = "{url}"
username = "{user}"
provider = "db"
verify_tls = false
timeout_seconds = 10

[safety]
max_rows = 100
hard_max_rows = 1000
query_timeout_seconds = 10
max_queries_per_minute = 60
max_concurrent_queries = 4
max_queries_per_day = 500
allow_delete = false

[reporting]
default_targets = ["local"]

[[databases]]
name = "acme_oms"
id = 3
default_schema = "public"
max_rows = 50
"""


@pytest.fixture(scope="session")
def mock_url():
    server, url = serve()
    yield url
    server.shutdown()


@pytest.fixture
def env(tmp_path, mock_url, monkeypatch):
    state = tmp_path / "state"
    watches = tmp_path / "watches"
    state.mkdir()
    watches.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(CONFIG_TEMPLATE.format(url=mock_url, user=VALID_USER))

    monkeypatch.setenv("TATHYA_CONFIG", str(config))
    monkeypatch.setenv("TATHYA_STATE_DIR", str(state))
    monkeypatch.setenv("TATHYA_WATCHES_DIR", str(watches))
    monkeypatch.setenv("TATHYA_ACTOR", "pytest")
    monkeypatch.delenv("TATHYA_ALLOW_WRITE", raising=False)

    # Never touch the developer's real Keychain during tests.
    import tathya.auth as auth_mod

    monkeypatch.setattr(auth_mod, "get_secret", lambda account: VALID_PASSWORD)
    RECEIVED.clear()
    return SimpleNamespace(config=config, state=state, watches=watches, url=mock_url, received=RECEIVED)


def run_cli(*argv: str) -> tuple[int, dict]:
    """Invoke the CLI in-process and parse its JSON envelope."""
    from tathya.cli import main

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(["--json", *argv])
    text = buffer.getvalue()
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, {"_raw": text}


@pytest.fixture
def cli():
    return run_cli
