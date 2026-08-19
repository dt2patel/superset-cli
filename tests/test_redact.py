from __future__ import annotations

from tathya.redact import MASK, redact, redact_text


def test_masks_secret_keys():
    out = redact({"sqlalchemy_uri": "postgresql://u:p@h/db", "password": "hunter2", "id": 3})
    assert out["sqlalchemy_uri"] == MASK
    assert out["password"] == MASK
    assert out["id"] == 3


def test_masks_dsn_password_inline():
    assert "sup3rs3cret" not in redact_text("postgresql://tathya:sup3rs3cret@replica/db")


def test_masks_jwt():
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZ2VudCJ9.abcdefghij"
    assert token not in redact_text(f"Authorization: Bearer {token}")


def test_recurses_into_lists_and_nested_dicts():
    out = redact({"result": [{"encrypted_extra": "x", "name": "ok"}]})
    assert out["result"][0]["encrypted_extra"] == MASK
    assert out["result"][0]["name"] == "ok"


def test_webhook_url_is_masked():
    out = redact({"webhook": "https://chat.googleapis.com/v1/spaces/AAA/messages?key=k&token=t"})
    assert out["webhook"] == MASK


def test_boolean_status_flags_are_not_masked():
    """`auth status` reports these; masking them destroys the answer."""
    out = redact({"password_in_keychain": False, "has_refresh_token": True})
    assert out["password_in_keychain"] is False
    assert out["has_refresh_token"] is True


def test_none_under_secret_key_stays_none():
    assert redact({"access_token": None})["access_token"] is None


def test_actual_secret_strings_still_masked_alongside_metadata():
    out = redact(
        {
            "password_in_keychain": True,
            "password": "hunter2",
            "token_count": 5,
            "access_token_expires_in_seconds": 812,
            "access_token": "eyJabc.eyJdef.sig",
        }
    )
    assert out["password_in_keychain"] is True
    assert out["token_count"] == 5                      # a count is not a secret
    assert out["access_token_expires_in_seconds"] == 812  # nor is an expiry
    assert out["password"] == MASK
    assert out["access_token"] == MASK


# --- Regressions from live verification against Tathya --------------------
# A loose substring match on key names masked three different useful values.
# Each of these is a real field this tool prints.

def test_rate_limit_budget_is_not_a_secret():
    """`limits` reports token-bucket budget; the word 'token' is incidental."""
    out = redact({"tokens_available": {"global": 3.5, "db:acme_oms": 2.0}})
    assert out["tokens_available"] == {"global": 3.5, "db:acme_oms": 2.0}


def test_auth_status_fields_survive():
    out = redact(
        {
            "password_in_keychain": True,
            "access_token_expires_in_seconds": 898,
            "has_refresh_token": True,
            "superset_user_id": 27,
        }
    )
    assert out == {
        "password_in_keychain": True,
        "access_token_expires_in_seconds": 898,
        "has_refresh_token": True,
        "superset_user_id": 27,
    }


def test_gchat_thread_key_is_not_masked():
    assert redact({"thread_key": "stuck-orders"})["thread_key"] == "stuck-orders"


def test_real_secrets_still_masked_after_tightening():
    out = redact(
        {
            "password": "hunter2",
            "access_token": "eyJa.eyJb.sig",
            "refresh_token": "eyJc.eyJd.sig",
            "sqlalchemy_uri": "postgresql://tathya:s3cret@replica/acme_oms",
            "masked_encrypted_extra": '{"k":"v"}',
            "db_password": "x",
            "webhook": "https://chat.googleapis.com/v1/spaces/A/messages?key=k&token=t",
            "authorization": "Bearer abc",
        }
    )
    assert all(v == MASK for v in out.values()), out
