"""
Tests for the `!pending` command (main.py), which lists every request
currently awaiting approval (status == 'unfinished'), most recently
requested first.

`main` is imported once in tests/conftest.py, with all required env vars
stubbed and arrapi.SonarrAPI / arrapi.RadarrAPI replaced by dummies before
import, so no network access or real credentials are needed to run these.
"""

from unittest.mock import AsyncMock, MagicMock

import main
from boto3.dynamodb.conditions import Key


def _make_ctx(author_id=123):
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.channel.send = AsyncMock()
    return ctx


def _query_mock(items=None):
    return MagicMock(return_value={'Items': items or []})


async def test_pending_no_items_sends_no_requests_message(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")
    monkeypatch.setattr(main.table, "query", _query_mock())

    ctx = _make_ctx()
    await main.pending(ctx)

    ctx.channel.send.assert_awaited_once_with("No requests are pending approval.")


async def test_pending_lists_items_with_title_type_and_requester(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")
    monkeypatch.setattr(main.table, "query", _query_mock(items=[
        {
            'PK': 'USER#111', 'SK': 'REQUEST#abc',
            'title': 'Some Movie', 'type': 'Movie',
            'status': 'unfinished', 'requestedAt': 100,
        },
        {
            'PK': 'USER#222', 'SK': 'REQUEST#def',
            'title': 'Some Show', 'type': 'TV',
            'status': 'unfinished', 'requestedAt': 200,
        },
    ]))

    ctx = _make_ctx()
    await main.pending(ctx)

    text = ctx.channel.send.await_args.args[0]
    assert "Some Movie" in text
    assert "Movie" in text
    assert "<@111>" in text
    assert "Some Show" in text
    assert "TV" in text
    assert "<@222>" in text


async def test_pending_orders_most_recent_first_regardless_of_input_order(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")
    monkeypatch.setattr(main.table, "query", _query_mock(items=[
        {
            'PK': 'USER#111', 'SK': 'REQUEST#older',
            'title': 'Older Request', 'type': 'Movie',
            'status': 'unfinished', 'requestedAt': 100,
        },
        {
            'PK': 'USER#222', 'SK': 'REQUEST#newer',
            'title': 'Newer Request', 'type': 'TV',
            'status': 'unfinished', 'requestedAt': 300,
        },
        {
            'PK': 'USER#333', 'SK': 'REQUEST#middle',
            'title': 'Middle Request', 'type': 'Movie',
            'status': 'unfinished', 'requestedAt': 200,
        },
    ]))

    ctx = _make_ctx()
    await main.pending(ctx)

    text = ctx.channel.send.await_args.args[0]
    lines = [line for line in text.splitlines() if line and line[0].isdigit()]
    assert len(lines) == 3
    assert "Newer Request" in lines[0]
    assert "Middle Request" in lines[1]
    assert "Older Request" in lines[2]


async def test_pending_queries_expected_index_and_status(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")
    query = _query_mock()
    monkeypatch.setattr(main.table, "query", query)

    ctx = _make_ctx()
    await main.pending(ctx)

    query.assert_called_once()
    kwargs = query.call_args.kwargs
    assert kwargs['IndexName'] == 'Status-index'
    assert kwargs['ScanIndexForward'] is False
    assert kwargs['KeyConditionExpression'] == Key('status').eq('unfinished')


async def test_pending_requires_admin_role(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    query = MagicMock()
    monkeypatch.setattr(main.table, "query", query)

    ctx = _make_ctx()
    await main.pending(ctx)

    query.assert_not_called()
    ctx.channel.send.assert_awaited_once_with("Insufficient Permissions")
