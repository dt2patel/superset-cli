"""Google Chat webhook URLs carry their credentials in the query string
(``?key=...&token=...``). Adding a thread key must not drop them."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from tathya.notify import gchat

WEBHOOK = "https://chat.googleapis.com/v1/spaces/SPACE/messages?key=KEY&token=TOKEN"
CONFIG = SimpleNamespace(reporting=SimpleNamespace(gchat_thread_key="", gchat_space_label=""))


@pytest.fixture
def posted(monkeypatch):
    sent = {}

    def fake_post(url, **kwargs):
        # Resolve the URL the way httpx would, whichever way the caller passes it.
        sent["url"] = str(httpx.Request("POST", url, params=kwargs.get("params")).url)
        return SimpleNamespace(status_code=200, text="")

    monkeypatch.setattr(gchat, "webhook_url", lambda: WEBHOOK)
    monkeypatch.setattr(gchat.httpx, "post", fake_post)
    return sent


def test_send_keeps_webhook_key_and_token(posted):
    result = gchat.send({"watch": "w", "status": "alert"}, CONFIG)

    assert result["delivered"] is True
    assert "key=KEY" in posted["url"]
    assert "token=TOKEN" in posted["url"]
    assert "threadKey=w" in posted["url"]


def test_send_text_keeps_webhook_key_and_token(posted):
    result = gchat.send_text("hi", CONFIG, thread_key="digest")

    assert result["delivered"] is True
    assert "key=KEY" in posted["url"]
    assert "token=TOKEN" in posted["url"]
    assert "threadKey=digest" in posted["url"]
