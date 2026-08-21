"""The setup wizard - the cold-install path.

Driven with scripted input against the mock instance, asserting the *config it
writes*, because the whole point of the wizard is to produce a correct config
from things a newcomer can actually supply.
"""

from __future__ import annotations

import builtins
import io
import tomllib

import pytest

from tathya.commands import setup as setup_mod
from tathya.config import load as load_config
from tathya.context import Context
from tathya.errors import EXIT_OK, ConfigError, UsageError
from tathya.output import Emitter


class Args:
    def __init__(self, **kw):
        self.force = kw.get("force", True)
        self.confirm = False


def drive(monkeypatch, answers: list[str], tmp_path, mock_url, **kw):
    """Run the wizard with a scripted answer queue."""
    queue = list(answers)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": queue.pop(0) if queue else "")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(setup_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup_mod.shutil, "which", lambda _n: "/usr/bin/security")
    # Never touch the real Keychain.
    monkeypatch.setattr(setup_mod, "get_secret", lambda _a: "test-password")
    monkeypatch.setattr(setup_mod, "set_secret_interactive", lambda *a, **k: None)
    import tathya.auth as auth_mod
    monkeypatch.setattr(auth_mod, "get_secret", lambda _a: "test-password")

    config = tmp_path / "config.toml"
    monkeypatch.setenv("TATHYA_CONFIG", str(config))
    monkeypatch.setenv("TATHYA_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TATHYA_WATCHES_DIR", str(tmp_path / "watches"))

    ctx = Context(config=None, emitter=Emitter("json", io.StringIO()), args=Args(**kw))
    code = setup_mod.cmd_setup(ctx)
    return code, config


def test_wizard_writes_a_working_config(monkeypatch, tmp_path, mock_url):
    code, config = drive(monkeypatch, [
        mock_url,        # base URL
        "db",            # provider
        "agent-test",    # username
        "n",             # do not replace stored password
        "",              # no database filter
        "1",             # pick the first database
        "250",           # its row cap
        "n",             # skip starter watches
        "n",             # skip chat webhook
    ], tmp_path, mock_url=mock_url)

    assert code == EXIT_OK
    raw = tomllib.loads(config.read_text())
    assert raw["instance"]["base_url"] == mock_url
    assert raw["instance"]["username"] == "agent-test"
    assert raw["instance"]["provider"] == "db"
    assert len(raw["databases"]) == 1
    # The wizard must pin the id and derive the dialect - the two things a
    # newcomer cannot know and which silently break queries when absent.
    db = raw["databases"][0]
    assert isinstance(db["id"], int)
    assert db["max_rows"] == 250
    assert db["dialect"] in {"postgres", "mysql"}
    # And the result must actually load.
    loaded = load_config(config)
    assert loaded.databases[0].dialect == db["dialect"]


def test_wizard_strips_a_pasted_page_path(monkeypatch, tmp_path, mock_url):
    """The mistake everyone makes: pasting the page you were looking at."""
    _code, config = drive(monkeypatch, [
        mock_url + "/superset/welcome/",
        "db", "agent-test", "n", "", "", "n", "n",
    ], tmp_path, mock_url=mock_url)
    assert tomllib.loads(config.read_text())["instance"]["base_url"] == mock_url


def test_wizard_rejects_a_bad_url_before_asking_for_secrets(monkeypatch, tmp_path, mock_url):
    with pytest.raises(ConfigError, match="Could not verify"):
        drive(monkeypatch, ["http://127.0.0.1:9"] * 4, tmp_path, mock_url=mock_url)


def test_wizard_refuses_an_unsupported_provider_then_accepts(monkeypatch, tmp_path, mock_url):
    _code, config = drive(monkeypatch, [
        mock_url, "google", "ldap", "agent-test", "n", "", "", "n", "n",
    ], tmp_path, mock_url=mock_url)
    assert tomllib.loads(config.read_text())["instance"]["provider"] == "ldap"


def test_wizard_survives_selecting_no_databases(monkeypatch, tmp_path, mock_url):
    code, config = drive(monkeypatch, [
        mock_url, "db", "agent-test", "n", "", "", "n", "n",
    ], tmp_path, mock_url=mock_url)
    assert code == EXIT_OK
    raw = tomllib.loads(config.read_text())
    assert raw.get("databases", []) == []
    load_config(config)  # still valid, just refuses queries


def test_wizard_ignores_out_of_range_choices(monkeypatch, tmp_path, mock_url):
    _code, config = drive(monkeypatch, [
        mock_url, "db", "agent-test", "n", "", "99,1", "300", "n", "n",
    ], tmp_path, mock_url=mock_url)
    dbs = tomllib.loads(config.read_text())["databases"]
    assert len(dbs) == 1 and dbs[0]["max_rows"] == 300


def test_wizard_requires_a_terminal(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    ctx = Context(config=None, emitter=Emitter("json", io.StringIO()), args=Args())
    with pytest.raises(UsageError, match="interactive"):
        setup_mod.cmd_setup(ctx)


def test_wizard_refuses_on_non_macos(monkeypatch, tmp_path, mock_url):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(setup_mod.platform, "system", lambda: "Linux")
    ctx = Context(config=None, emitter=Emitter("json", io.StringIO()), args=Args())
    with pytest.raises(ConfigError, match="macOS Keychain"):
        setup_mod.cmd_setup(ctx)
