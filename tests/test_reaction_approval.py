"""
Tests for `on_raw_reaction_add` (main.py), the event handler that approves
or denies a pending request when an Admin+ reacts with ✅/❌ on its message.

`main.table.get_item` is called twice by this handler with different keys:
first to resolve the MESSAGE#<id> -> mediaPK mapping, then to fetch the
actual MEDIA# request item. Tests that need to reach the approve/deny
branches supply both results (in that order) via a `MagicMock(side_effect=[...])`.

`log_request_event` itself is patched out with an AsyncMock for every test
here -- its internals are covered separately in test_log_request_event.py --
so these tests only assert it was *called* with the right guild/message.

`main` is imported once in tests/conftest.py, with all required env vars
stubbed and arrapi.SonarrAPI / arrapi.RadarrAPI replaced by dummies before
import, so no network access or real credentials are needed to run these.
"""

import time
from unittest.mock import AsyncMock, MagicMock

import main


def _make_payload(user_id=555, message_id=999, guild_id=777, emoji_name="✅"):
    payload = MagicMock()
    payload.user_id = user_id
    payload.message_id = message_id
    payload.guild_id = guild_id
    payload.emoji.name = emoji_name
    return payload


def _setup_common(monkeypatch, bot_user_id=1, reactor_role="ADMIN", fake_guild=None):
    # discord.py's Bot.user is a read-only property (backed by the internal
    # connection state), so it can't be set on the instance -- patch it at
    # the class level instead, which monkeypatch still restores afterward.
    monkeypatch.setattr(type(main.bot), "user", MagicMock(id=bot_user_id), raising=False)
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: reactor_role)
    if fake_guild is None:
        fake_guild = MagicMock()
    monkeypatch.setattr(main.bot, "get_guild", lambda gid: fake_guild)
    monkeypatch.setattr(main, "log_request_event", AsyncMock())
    return fake_guild


# --- Early-return / no-op paths ---

async def test_reaction_bot_own_reaction_is_ignored(monkeypatch):
    _setup_common(monkeypatch, bot_user_id=42)
    get_item = MagicMock()
    monkeypatch.setattr(main.table, "get_item", get_item)

    payload = _make_payload(user_id=42)
    await main.on_raw_reaction_add(payload)

    get_item.assert_not_called()
    main.log_request_event.assert_not_called()


async def test_reaction_non_target_emoji_is_ignored(monkeypatch):
    _setup_common(monkeypatch)
    get_item = MagicMock()
    monkeypatch.setattr(main.table, "get_item", get_item)

    payload = _make_payload(emoji_name="👍")
    await main.on_raw_reaction_add(payload)

    get_item.assert_not_called()
    main.log_request_event.assert_not_called()


async def test_reaction_below_admin_role_is_ignored(monkeypatch):
    _setup_common(monkeypatch, reactor_role="TRUSTED")
    get_item = MagicMock()
    monkeypatch.setattr(main.table, "get_item", get_item)

    payload = _make_payload()
    await main.on_raw_reaction_add(payload)

    get_item.assert_not_called()
    main.log_request_event.assert_not_called()


async def test_reaction_no_matching_pending_message_is_noop(monkeypatch):
    _setup_common(monkeypatch)
    monkeypatch.setattr(main.table, "get_item", MagicMock(return_value={}))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    payload = _make_payload()
    await main.on_raw_reaction_add(payload)

    update_item.assert_not_called()
    main.log_request_event.assert_not_called()


async def test_reaction_media_item_missing_is_noop(monkeypatch):
    _setup_common(monkeypatch)
    monkeypatch.setattr(main.table, "get_item", MagicMock(side_effect=[
        {'Item': {'mediaPK': 'MEDIA#MOVIE#tt123'}},
        {},  # no media item found
    ]))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    payload = _make_payload()
    await main.on_raw_reaction_add(payload)

    update_item.assert_not_called()
    main.log_request_event.assert_not_called()


async def test_reaction_media_item_already_resolved_is_noop(monkeypatch):
    _setup_common(monkeypatch)
    monkeypatch.setattr(main.table, "get_item", MagicMock(side_effect=[
        {'Item': {'mediaPK': 'MEDIA#MOVIE#tt123'}},
        {'Item': {'status': 'approved', 'title': 'Already Done'}},
    ]))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    payload = _make_payload()
    await main.on_raw_reaction_add(payload)

    update_item.assert_not_called()
    main.log_request_event.assert_not_called()


# --- Approve path ---

async def test_reaction_approve_updates_both_items_and_logs(monkeypatch):
    fake_guild = _setup_common(monkeypatch)

    message_item = {'mediaPK': 'MEDIA#MOVIE#tt123'}
    media_item = {
        'title': 'Some Movie',
        'status': 'unfinished',
        'requestedBy': '111',
        'requestId': 'abc-123',
    }
    monkeypatch.setattr(main.table, "get_item", MagicMock(side_effect=[
        {'Item': message_item},
        {'Item': media_item},
    ]))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    payload = _make_payload(user_id=555, emoji_name="✅", guild_id=777)
    await main.on_raw_reaction_add(payload)

    assert update_item.call_count == 2

    media_call = update_item.call_args_list[0]
    assert media_call.kwargs['Key'] == {'PK': 'MEDIA#MOVIE#tt123', 'SK': 'REQUEST'}
    assert media_call.kwargs['UpdateExpression'] == "SET #s = :new_status, approvedBy = :approver"
    assert media_call.kwargs['ExpressionAttributeValues'] == {
        ':new_status': 'approved',
        ':approver': '555',
    }
    assert 'expiresAt' not in media_call.kwargs['UpdateExpression']

    user_call = update_item.call_args_list[1]
    assert user_call.kwargs['Key'] == {'PK': 'USER#111', 'SK': 'REQUEST#abc-123'}
    assert user_call.kwargs['UpdateExpression'] == "SET #s = :new_status, approvedBy = :approver"
    assert user_call.kwargs['ExpressionAttributeValues'] == {
        ':new_status': 'approved',
        ':approver': '555',
    }
    assert 'expiresAt' not in user_call.kwargs['UpdateExpression']

    main.log_request_event.assert_awaited_once()
    log_args = main.log_request_event.await_args.args
    assert log_args[0] is fake_guild
    assert "approved" in log_args[1]
    assert "Some Movie" in log_args[1]
    assert "555" in log_args[1]


# --- Deny path ---

async def test_reaction_deny_updates_both_items_with_expiry_and_logs(monkeypatch):
    fake_guild = _setup_common(monkeypatch)

    message_item = {'mediaPK': 'MEDIA#MOVIE#tt456'}
    media_item = {
        'title': 'Another Movie',
        'status': 'unfinished',
        'requestedBy': '222',
        'requestId': 'def-456',
    }
    monkeypatch.setattr(main.table, "get_item", MagicMock(side_effect=[
        {'Item': message_item},
        {'Item': media_item},
    ]))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    before = int(time.time())
    payload = _make_payload(user_id=666, emoji_name="❌", guild_id=777)
    await main.on_raw_reaction_add(payload)
    after = int(time.time())

    expected_expires_low = before + main.DENIED_ITEM_TTL_DAYS * main.SECONDS_PER_DAY
    expected_expires_high = after + main.DENIED_ITEM_TTL_DAYS * main.SECONDS_PER_DAY

    assert update_item.call_count == 2

    media_call = update_item.call_args_list[0]
    assert media_call.kwargs['Key'] == {'PK': 'MEDIA#MOVIE#tt456', 'SK': 'REQUEST'}
    assert media_call.kwargs['UpdateExpression'] == (
        "SET #s = :new_status, deniedBy = :denier, expiresAt = :expires_at"
    )
    media_values = media_call.kwargs['ExpressionAttributeValues']
    assert media_values[':new_status'] == 'denied'
    assert media_values[':denier'] == '666'
    assert expected_expires_low <= media_values[':expires_at'] <= expected_expires_high

    user_call = update_item.call_args_list[1]
    assert user_call.kwargs['Key'] == {'PK': 'USER#222', 'SK': 'REQUEST#def-456'}
    assert user_call.kwargs['UpdateExpression'] == (
        "SET #s = :new_status, deniedBy = :denier, expiresAt = :expires_at"
    )
    user_values = user_call.kwargs['ExpressionAttributeValues']
    assert user_values[':new_status'] == 'denied'
    assert user_values[':denier'] == '666'
    assert expected_expires_low <= user_values[':expires_at'] <= expected_expires_high

    main.log_request_event.assert_awaited_once()
    log_args = main.log_request_event.await_args.args
    assert log_args[0] is fake_guild
    assert "denied" in log_args[1]
    assert "Another Movie" in log_args[1]
    assert "666" in log_args[1]
