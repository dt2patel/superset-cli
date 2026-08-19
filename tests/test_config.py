"""Config validation - a wrong validator here blocks every query."""

from __future__ import annotations

import pytest

from tathya.config import load
from tathya.errors import ConfigError

BASE = '''
[instance]
base_url = "https://example.invalid"
username = "u"
provider = "{provider}"
[[databases]]
name = "db1"
id = 1
{extra}
'''


def write(tmp_path, provider="db", extra=""):
    path = tmp_path / "config.toml"
    path.write_text(BASE.format(provider=provider, extra=extra))
    return path


@pytest.mark.parametrize("dialect", ["mysql", "postgres", "tsql", "sqlite", "trino", "bigquery"])
def test_real_dialects_are_accepted(tmp_path, dialect):
    """These are the backends Tathya actually uses; rejecting one blocks all queries."""
    cfg = load(write(tmp_path, extra=f'dialect = "{dialect}"'))
    assert cfg.databases[0].dialect == dialect


def test_dialect_is_case_insensitive(tmp_path):
    assert load(write(tmp_path, extra='dialect = "MySQL"')).databases[0].dialect == "mysql"


def test_unknown_dialect_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="Unknown sqlglot dialect"):
        load(write(tmp_path, extra='dialect = "klingon"'))


def test_dialect_is_optional(tmp_path):
    assert load(write(tmp_path)).databases[0].dialect is None


@pytest.mark.parametrize("provider", ["db", "ldap"])
def test_supported_providers(tmp_path, provider):
    assert load(write(tmp_path, provider=provider)).instance.provider == provider


def test_oauth_provider_rejected_with_explanation(tmp_path):
    with pytest.raises(ConfigError, match="db' or 'ldap"):
        load(write(tmp_path, provider="google"))


def test_row_cap_takes_the_tighter_of_global_and_per_database(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('''
[instance]
base_url = "https://example.invalid"
username = "u"
[safety]
max_rows = 500
[[databases]]
name = "tight"
max_rows = 50
[[databases]]
name = "loose"
max_rows = 5000
''')
    cfg = load(path)
    assert cfg.row_cap_for(cfg.database_rule("tight")) == 50
    assert cfg.row_cap_for(cfg.database_rule("loose")) == 500  # global still wins
