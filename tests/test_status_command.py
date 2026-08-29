"""
Tests for the `!status` command (main.py), which shows current download
progress for the last 10 requested items.

`main` is imported once in tests/conftest.py, with all required env vars
stubbed and arrapi.SonarrAPI / arrapi.RadarrAPI replaced by dummies before
import, so no network access or real credentials are needed to run these.

`main.table.query` is called three times by `status` -- once per GSI
`status` value, in the fixed order "unfinished", "approved", "denied" --
so each test's `MagicMock(side_effect=[...])` supplies results in that
same order.
"""

from unittest.mock import AsyncMock, MagicMock

import main
from arrapi.exceptions import NotFound


def _make_ctx(author_id=123):
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.channel.send = AsyncMock()
    return ctx


def _query_mock(unfinished=None, approved=None, denied=None):
    def _items(items):
        return {'Items': items or []}

    return MagicMock(side_effect=[
        _items(unfinished),
        _items(approved),
        _items(denied),
    ])


async def test_status_no_requests_sends_no_requests_message(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    monkeypatch.setattr(main.table, "query", _query_mock())

    ctx = _make_ctx()
    await main.status(ctx)

    ctx.channel.send.assert_awaited_once_with("No requests yet.")


async def test_status_pending_request(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    monkeypatch.setattr(main.table, "query", _query_mock(
        unfinished=[{
            'PK': 'USER#123', 'SK': 'REQUEST#1',
            'title': 'Some Movie', 'imdbID': 'tt0000001', 'type': 'Movie',
            'status': 'unfinished', 'requestedAt': 100,
        }],
    ))

    ctx = _make_ctx()
    await main.status(ctx)

    text = ctx.channel.send.await_args.args[0]
    assert "Some Movie (Movie) — ⏳ Pending approval" in text


async def test_status_denied_request(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    monkeypatch.setattr(main.table, "query", _query_mock(
        denied=[{
            'PK': 'USER#123', 'SK': 'REQUEST#2',
            'title': 'Rejected Movie', 'imdbID': 'tt0000002', 'type': 'Movie',
            'status': 'denied', 'requestedAt': 90,
        }],
    ))

    ctx = _make_ctx()
    await main.status(ctx)

    text = ctx.channel.send.await_args.args[0]
    assert "Rejected Movie (Movie) — ❌ Denied" in text


async def test_status_downloaded_movie(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    monkeypatch.setattr(main.table, "query", _query_mock(
        approved=[{
            'PK': 'USER#123', 'SK': 'REQUEST#3',
            'title': 'The Invite', 'imdbID': 'tt0000003', 'type': 'Movie',
            'status': 'approved', 'requestedAt': 200,
        }],
    ))

    fake_movie = MagicMock()
    fake_movie.id = 5
    fake_movie.hasFile = True
    monkeypatch.setattr(main.radarr, "get_movie", MagicMock(return_value=fake_movie))
    monkeypatch.setattr(main.radarr._raw, "_get", MagicMock(return_value={'records': []}))

    ctx = _make_ctx()
    await main.status(ctx)

    text = ctx.channel.send.await_args.args[0]
    assert "The Invite (Movie) — ✅ Downloaded" in text


async def test_status_movie_not_found_in_radarr(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    monkeypatch.setattr(main.table, "query", _query_mock(
        approved=[{
            'PK': 'USER#123', 'SK': 'REQUEST#4',
            'title': 'Vanished Movie', 'imdbID': 'tt0000004', 'type': 'Movie',
            'status': 'approved', 'requestedAt': 150,
        }],
    ))

    monkeypatch.setattr(main.radarr, "get_movie", MagicMock(side_effect=NotFound("nope")))
    monkeypatch.setattr(main.radarr._raw, "_get", MagicMock(return_value={'records': []}))

    ctx = _make_ctx()
    await main.status(ctx)

    text = ctx.channel.send.await_args.args[0]
    assert "Vanished Movie (Movie) — ⚠️ Not found in Radarr" in text


async def test_status_movie_searching_no_queue_record(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    monkeypatch.setattr(main.table, "query", _query_mock(
        approved=[{
            'PK': 'USER#123', 'SK': 'REQUEST#5',
            'title': 'Not Yet Movie', 'imdbID': 'tt0000005', 'type': 'Movie',
            'status': 'approved', 'requestedAt': 140,
        }],
    ))

    fake_movie = MagicMock()
    fake_movie.id = 7
    fake_movie.hasFile = False
    monkeypatch.setattr(main.radarr, "get_movie", MagicMock(return_value=fake_movie))
    monkeypatch.setattr(main.radarr._raw, "_get", MagicMock(return_value={'records': []}))

    ctx = _make_ctx()
    await main.status(ctx)

    text = ctx.channel.send.await_args.args[0]
    assert "Not Yet Movie (Movie) — 🔍 Searching / not yet downloading" in text


async def test_status_in_progress_tv_show_percent(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    monkeypatch.setattr(main.table, "query", _query_mock(
        approved=[{
            'PK': 'USER#123', 'SK': 'REQUEST#6',
            'title': 'Ted Lasso', 'tvdbID': 12345, 'type': 'TV',
            'status': 'approved', 'requestedAt': 300,
        }],
    ))

    fake_series = MagicMock()
    fake_series.id = 42
    fake_series.episodeFileCount = 3
    monkeypatch.setattr(main.sonarr, "get_series", MagicMock(return_value=fake_series))

    size = 13.0 * (1024 ** 3)
    sizeleft = size - (8.2 * (1024 ** 3))
    monkeypatch.setattr(main.sonarr._raw, "_get", MagicMock(return_value={
        'records': [{'seriesId': 42, 'size': size, 'sizeleft': sizeleft}]
    }))

    ctx = _make_ctx()
    await main.status(ctx)

    text = ctx.channel.send.await_args.args[0]
    assert "Ted Lasso (TV) — ⬇️ 63% (8.2GB / 13.0GB)" in text


async def test_status_tv_downloaded_no_active_queue_record(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    monkeypatch.setattr(main.table, "query", _query_mock(
        approved=[{
            'PK': 'USER#123', 'SK': 'REQUEST#7',
            'title': 'Finished Show', 'tvdbID': 54321, 'type': 'TV',
            'status': 'approved', 'requestedAt': 310,
        }],
    ))

    fake_series = MagicMock()
    fake_series.id = 99
    fake_series.episodeFileCount = 10
    monkeypatch.setattr(main.sonarr, "get_series", MagicMock(return_value=fake_series))
    monkeypatch.setattr(main.sonarr._raw, "_get", MagicMock(return_value={'records': []}))

    ctx = _make_ctx()
    await main.status(ctx)

    text = ctx.channel.send.await_args.args[0]
    assert "Finished Show (TV) — ✅ Downloaded" in text


async def test_status_orders_and_limits_to_ten_most_recent(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")

    unfinished = [
        {
            'PK': 'USER#123', 'SK': f'REQUEST#{i}',
            'title': f'Item {i}', 'imdbID': f'tt{i:07d}', 'type': 'Movie',
            'status': 'unfinished', 'requestedAt': i,
        }
        for i in range(12)
    ]
    monkeypatch.setattr(main.table, "query", _query_mock(unfinished=unfinished))

    ctx = _make_ctx()
    await main.status(ctx)

    text = ctx.channel.send.await_args.args[0]
    lines = [line for line in text.splitlines() if line and line[0].isdigit()]
    assert len(lines) == 10
    # newest (highest requestedAt) first
    assert "Item 11" in lines[0]
    assert "Item 2" in lines[9]
