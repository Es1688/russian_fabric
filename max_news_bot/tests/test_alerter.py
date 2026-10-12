"""MaxAlerter tests: incident alerts must reach the admins' DMs.

admin_ids hold user_id, so every send is addressed by the user_id query
parameter — a chat_id would make the API look for a group chat.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from max_news_bot.publishers.alerter import MaxAlerter

_ADMINS = [111, 222]


def make_client() -> AsyncMock:
    client = AsyncMock()
    client.send_message.return_value = "mid.1"
    return client


async def test_alert_and_recovery_addressed_by_user_id() -> None:
    client = make_client()
    alerter = MaxAlerter(client, list(_ADMINS))

    await alerter.send_alert("disk full")
    await alerter.send_recovery("disk ok")

    assert client.send_message.await_count == 2 * len(_ADMINS)
    recipients = [
        call.kwargs["user_id"] for call in client.send_message.await_args_list
    ]
    assert recipients == _ADMINS + _ADMINS
    for call in client.send_message.await_args_list:
        # no chat_id in the addressing: args hold only text-less positionals
        assert not call.args
        assert call.kwargs["fmt"] == "html"


async def test_one_admin_failure_does_not_break_the_rest() -> None:
    client = make_client()
    client.send_message.side_effect = [RuntimeError("dm closed"), "mid.1"]
    alerter = MaxAlerter(client, list(_ADMINS))

    await alerter.send_alert("boom")  # must not raise

    assert client.send_message.await_count == 2
