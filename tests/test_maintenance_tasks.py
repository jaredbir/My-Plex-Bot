"""
Tests for the background maintenance system in main.py:
`check_available_requests`, `sweep_stale_requests`, and the
`periodic_maintenance` tasks.loop that wraps both of them.

`main` is imported once in tests/conftest.py, with all required env vars
stubbed and arrapi.SonarrAPI / arrapi.RadarrAPI replaced by dummies before
import, so no network access or real credentials are needed to run these.
"""

import time

from unittest.mock import AsyncMock, MagicMock

import main
from arrapi.exceptions import NotFound


def _query_mock(items=None):
    """main.table.query is called once per sweep function, so a single
    canned response (no side_effect list needed) covers each call."""
    return MagicMock(return_value={'Items': items or []})


def _set_guilds(monkeypatch, guilds):
    """discord.Client.guilds is a read-only property (backed by internal
    connection state), so it can't be monkeypatched directly on the `bot`
    instance -- patch the property on the class instead."""
    monkeypatch.setattr(type(main.bot), "guilds", property(lambda self: guilds))


# --- check_available_requests ---

async def test_check_available_movie_hasfile_notifies_and_marks(monkeypatch):
    item = {
        'PK': 'USER#111', 'SK': 'REQUEST#1',
        'title': 'Some Movie', 'imdbID': 'tt1234567', 'type': 'Movie',
        'status': 'approved',
    }
    monkeypatch.setattr(main.table, "query", _query_mock([item]))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    fake_movie = MagicMock()
    fake_movie.hasFile = True
    monkeypatch.setattr(main.radarr, "get_movie", MagicMock(return_value=fake_movie))

    log_event = AsyncMock()
    monkeypatch.setattr(main, "log_request_event", log_event)
    _set_guilds(monkeypatch, [MagicMock()])

    await main.check_available_requests()

    log_event.assert_awaited_once()
    message = log_event.await_args.args[1]
    assert "🎬" in message
    assert "Some Movie" in message
    assert "<@111>" in message

    update_item.assert_called_once_with(
        Key={'PK': 'USER#111', 'SK': 'REQUEST#1'},
        UpdateExpression="SET notifiedAvailable = :true_val",
        ExpressionAttributeValues={':true_val': True}
    )


async def test_check_available_movie_no_file_does_nothing(monkeypatch):
    item = {
        'PK': 'USER#111', 'SK': 'REQUEST#1',
        'title': 'Some Movie', 'imdbID': 'tt1234567', 'type': 'Movie',
        'status': 'approved',
    }
    monkeypatch.setattr(main.table, "query", _query_mock([item]))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    fake_movie = MagicMock()
    fake_movie.hasFile = False
    monkeypatch.setattr(main.radarr, "get_movie", MagicMock(return_value=fake_movie))

    log_event = AsyncMock()
    monkeypatch.setattr(main, "log_request_event", log_event)
    _set_guilds(monkeypatch, [MagicMock()])

    await main.check_available_requests()

    log_event.assert_not_awaited()
    update_item.assert_not_called()


async def test_check_available_tv_episode_file_count_truthy_notifies(monkeypatch):
    item = {
        'PK': 'USER#222', 'SK': 'REQUEST#2',
        'title': 'Some Show', 'tvdbID': 54321, 'type': 'TV',
        'status': 'approved',
    }
    monkeypatch.setattr(main.table, "query", _query_mock([item]))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    fake_series = MagicMock()
    fake_series.episodeFileCount = 5
    monkeypatch.setattr(main.sonarr, "get_series", MagicMock(return_value=fake_series))

    log_event = AsyncMock()
    monkeypatch.setattr(main, "log_request_event", log_event)
    _set_guilds(monkeypatch, [MagicMock()])

    await main.check_available_requests()

    log_event.assert_awaited_once()
    update_item.assert_called_once()


async def test_check_available_tv_episode_file_count_falsy_skips(monkeypatch):
    item = {
        'PK': 'USER#222', 'SK': 'REQUEST#2',
        'title': 'Some Show', 'tvdbID': 54321, 'type': 'TV',
        'status': 'approved',
    }
    monkeypatch.setattr(main.table, "query", _query_mock([item]))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    fake_series = MagicMock()
    fake_series.episodeFileCount = 0
    monkeypatch.setattr(main.sonarr, "get_series", MagicMock(return_value=fake_series))

    log_event = AsyncMock()
    monkeypatch.setattr(main, "log_request_event", log_event)
    _set_guilds(monkeypatch, [MagicMock()])

    await main.check_available_requests()

    log_event.assert_not_awaited()
    update_item.assert_not_called()


async def test_check_available_movie_not_found_skips_cleanly(monkeypatch):
    item = {
        'PK': 'USER#333', 'SK': 'REQUEST#3',
        'title': 'Vanished Movie', 'imdbID': 'tt9999999', 'type': 'Movie',
        'status': 'approved',
    }
    monkeypatch.setattr(main.table, "query", _query_mock([item]))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    monkeypatch.setattr(main.radarr, "get_movie", MagicMock(side_effect=NotFound("nope")))

    log_event = AsyncMock()
    monkeypatch.setattr(main, "log_request_event", log_event)
    _set_guilds(monkeypatch, [MagicMock()])

    await main.check_available_requests()

    log_event.assert_not_awaited()
    update_item.assert_not_called()


async def test_check_available_notifies_every_guild(monkeypatch):
    item = {
        'PK': 'USER#444', 'SK': 'REQUEST#4',
        'title': 'Multi Guild Movie', 'imdbID': 'tt1111111', 'type': 'Movie',
        'status': 'approved',
    }
    monkeypatch.setattr(main.table, "query", _query_mock([item]))
    monkeypatch.setattr(main.table, "update_item", MagicMock())

    fake_movie = MagicMock()
    fake_movie.hasFile = True
    monkeypatch.setattr(main.radarr, "get_movie", MagicMock(return_value=fake_movie))

    log_event = AsyncMock()
    monkeypatch.setattr(main, "log_request_event", log_event)

    guild1, guild2 = MagicMock(), MagicMock()
    _set_guilds(monkeypatch, [guild1, guild2])

    await main.check_available_requests()

    assert log_event.await_count == 2
    called_guilds = [call.args[0] for call in log_event.await_args_list]
    assert called_guilds == [guild1, guild2]


# --- sweep_stale_requests ---

async def test_sweep_stale_item_sets_expiry_on_user_and_media(monkeypatch):
    old_requested_at = int(time.time()) - 20 * main.SECONDS_PER_DAY
    item = {
        'PK': 'USER#555', 'SK': 'REQUEST#5',
        'title': 'Old Request', 'imdbID': 'tt1234567', 'type': 'Movie',
        'status': 'unfinished', 'requestedAt': old_requested_at,
    }
    monkeypatch.setattr(main.table, "query", _query_mock([item]))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    await main.sweep_stale_requests()

    assert update_item.call_count == 2

    user_call, media_call = update_item.call_args_list

    assert user_call.kwargs['Key'] == {'PK': 'USER#555', 'SK': 'REQUEST#5'}
    assert user_call.kwargs['UpdateExpression'] == "SET expiresAt = :expires_at"
    assert 'expiresAt' not in item  # sanity: original item dict untouched by shape check
    expires_at_value = user_call.kwargs['ExpressionAttributeValues'][':expires_at']

    assert media_call.kwargs['Key'] == {'PK': 'MEDIA#MOVIE#tt1234567', 'SK': 'REQUEST'}
    assert media_call.kwargs['UpdateExpression'] == "SET expiresAt = :expires_at"
    assert media_call.kwargs['ExpressionAttributeValues'][':expires_at'] == expires_at_value


async def test_sweep_recent_item_is_not_stale(monkeypatch):
    recent_requested_at = int(time.time()) - 1 * main.SECONDS_PER_DAY
    item = {
        'PK': 'USER#666', 'SK': 'REQUEST#6',
        'title': 'Recent Request', 'imdbID': 'tt7654321', 'type': 'Movie',
        'status': 'unfinished', 'requestedAt': recent_requested_at,
    }
    monkeypatch.setattr(main.table, "query", _query_mock([item]))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    await main.sweep_stale_requests()

    update_item.assert_not_called()


async def test_sweep_stale_item_without_media_pk_only_updates_user_item(monkeypatch):
    old_requested_at = int(time.time()) - 20 * main.SECONDS_PER_DAY
    item = {
        'PK': 'USER#777', 'SK': 'REQUEST#7',
        'title': 'No Media Link Request', 'type': 'Unknown',
        'status': 'unfinished', 'requestedAt': old_requested_at,
    }
    monkeypatch.setattr(main.table, "query", _query_mock([item]))
    update_item = MagicMock()
    monkeypatch.setattr(main.table, "update_item", update_item)

    await main.sweep_stale_requests()

    update_item.assert_called_once()
    call = update_item.call_args
    assert call.kwargs['Key'] == {'PK': 'USER#777', 'SK': 'REQUEST#7'}


# --- periodic_maintenance ---

async def test_periodic_maintenance_calls_both(monkeypatch):
    check_mock = AsyncMock()
    sweep_mock = AsyncMock()
    monkeypatch.setattr(main, "check_available_requests", check_mock)
    monkeypatch.setattr(main, "sweep_stale_requests", sweep_mock)

    await main.periodic_maintenance.coro()

    check_mock.assert_awaited_once()
    sweep_mock.assert_awaited_once()


async def test_periodic_maintenance_isolates_check_failure(monkeypatch):
    check_mock = AsyncMock(side_effect=RuntimeError("boom"))
    sweep_mock = AsyncMock()
    monkeypatch.setattr(main, "check_available_requests", check_mock)
    monkeypatch.setattr(main, "sweep_stale_requests", sweep_mock)

    await main.periodic_maintenance.coro()  # must not raise

    check_mock.assert_awaited_once()
    sweep_mock.assert_awaited_once()


async def test_periodic_maintenance_isolates_sweep_failure(monkeypatch):
    check_mock = AsyncMock()
    sweep_mock = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(main, "check_available_requests", check_mock)
    monkeypatch.setattr(main, "sweep_stale_requests", sweep_mock)

    await main.periodic_maintenance.coro()  # must not raise

    check_mock.assert_awaited_once()
    sweep_mock.assert_awaited_once()
