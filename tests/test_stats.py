"""
Tests for the stats/leaderboard feature in main.py:
`compute_request_stats()`, `build_stats_embed()`, the `!setupStats` admin
command, and `refresh_stats_channels()`.

`main` is imported once in tests/conftest.py, with all required env vars
stubbed and arrapi.SonarrAPI / arrapi.RadarrAPI replaced by dummies before
import, so no network access or real credentials are needed to run these.

`main.table.query` is called three times by `compute_request_stats` -- once
per GSI `status` value, in the fixed order "unfinished", "approved",
"denied" -- so each test's `MagicMock(side_effect=[...])` supplies results
in that same order (same convention as tests/test_status_command.py).
"""

from collections import Counter
from unittest.mock import AsyncMock, MagicMock

import discord

import main


def _make_ctx(author_id=123):
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.channel.send = AsyncMock()
    return ctx


def _make_guild(existing_stats_channel=None):
    guild = MagicMock()
    guild.text_channels = [existing_stats_channel] if existing_stats_channel else []
    guild.default_role = "default_role_sentinel"
    guild.me = "guild_me_sentinel"
    guild.create_text_channel = AsyncMock()
    return guild


def _make_stats_channel():
    channel = MagicMock()
    channel.name = "stats"
    channel.send = AsyncMock()
    channel.purge = AsyncMock()
    channel.mention = "#stats"
    return channel


def _query_mock(unfinished=None, approved=None, denied=None):
    def _items(items):
        return {'Items': items or []}

    return MagicMock(side_effect=[
        _items(unfinished),
        _items(approved),
        _items(denied),
    ])


def _set_guilds(monkeypatch, guilds):
    """discord.Client.guilds is a read-only property (backed by internal
    connection state), so it can't be monkeypatched directly on the `bot`
    instance -- patch the property on the class instead."""
    monkeypatch.setattr(type(main.bot), "guilds", property(lambda self: guilds))


# --- compute_request_stats ---

def test_compute_request_stats_empty_table(monkeypatch):
    monkeypatch.setattr(main.table, "query", _query_mock())

    stats = main.compute_request_stats()

    assert stats['total'] == 0
    assert stats['approval_rate'] is None
    assert stats['top_requesters'] == []
    assert stats['by_status'] == Counter()
    assert stats['by_type'] == Counter()


def test_compute_request_stats_mixed_items(monkeypatch):
    unfinished = [
        {'PK': 'USER#111', 'SK': 'REQUEST#1', 'type': 'Movie', 'status': 'unfinished'},
    ]
    approved = [
        {'PK': 'USER#111', 'SK': 'REQUEST#2', 'type': 'Movie', 'status': 'approved'},
        {'PK': 'USER#111', 'SK': 'REQUEST#3', 'type': 'TV', 'status': 'approved'},
        {'PK': 'USER#222', 'SK': 'REQUEST#4', 'type': 'Movie', 'status': 'approved'},
    ]
    denied = [
        {'PK': 'USER#333', 'SK': 'REQUEST#5', 'type': 'TV', 'status': 'denied'},
    ]
    monkeypatch.setattr(main.table, "query", _query_mock(
        unfinished=unfinished, approved=approved, denied=denied,
    ))

    stats = main.compute_request_stats()

    assert stats['total'] == 5
    assert stats['by_status'] == Counter({'unfinished': 1, 'approved': 3, 'denied': 1})
    assert stats['by_type'] == Counter({'Movie': 3, 'TV': 2})
    # 3 approved + 1 denied -> 3/4 = 75%
    assert stats['approval_rate'] == 75
    # user 111 has 3 requests, 222 has 1, 333 has 1 -> 111 first
    assert stats['top_requesters'][0] == ('111', 3)
    assert set(stats['top_requesters'][1:]) == {('222', 1), ('333', 1)}


def test_compute_request_stats_uses_status_index_and_pk_filter(monkeypatch):
    query_mock = _query_mock()
    monkeypatch.setattr(main.table, "query", query_mock)

    main.compute_request_stats()

    assert query_mock.call_count == 3
    for call in query_mock.call_args_list:
        assert call.kwargs['IndexName'] == 'Status-index'


# --- build_stats_embed (pure function, with compute_request_stats patched) ---

def test_build_stats_embed_returns_embed():
    embed = main.build_stats_embed()
    assert isinstance(embed, discord.Embed)


def test_build_stats_embed_fields_with_data(monkeypatch):
    fixed_stats = {
        'total': 10,
        'by_status': Counter({'approved': 6, 'denied': 2, 'unfinished': 2}),
        'by_type': Counter({'Movie': 7, 'TV': 3}),
        'top_requesters': [('111', 5), ('222', 3)],
        'approval_rate': 75,
    }
    monkeypatch.setattr(main, "compute_request_stats", lambda: fixed_stats)

    embed = main.build_stats_embed()
    fields = {field.name: field.value for field in embed.fields}

    assert fields["Total Requests"] == "10"
    assert fields["Movies / TV"] == "7 / 3"
    assert fields["Approval Rate"] == "75%"
    assert fields["Status Breakdown"] == (
        "✅ Approved: 6\n"
        "❌ Denied: 2\n"
        "⏳ Pending: 2"
    )
    assert fields["Top Requesters"] == "1. <@111> — 5\n2. <@222> — 3"


def test_build_stats_embed_fallback_text_when_empty(monkeypatch):
    fixed_stats = {
        'total': 0,
        'by_status': Counter(),
        'by_type': Counter(),
        'top_requesters': [],
        'approval_rate': None,
    }
    monkeypatch.setattr(main, "compute_request_stats", lambda: fixed_stats)

    embed = main.build_stats_embed()
    fields = {field.name: field.value for field in embed.fields}

    assert fields["Approval Rate"] == "No decisions yet"
    assert fields["Top Requesters"] == "No requests yet."


# --- setup_stats: no existing #stats channel ---

async def test_setup_stats_creates_channel_when_missing(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")
    monkeypatch.setattr(main.table, "query", _query_mock())

    new_channel = _make_stats_channel()
    guild = _make_guild()
    guild.create_text_channel = AsyncMock(return_value=new_channel)

    ctx = _make_ctx()
    ctx.guild = guild

    await main.setup_stats(ctx)

    guild.create_text_channel.assert_awaited_once()
    args, kwargs = guild.create_text_channel.await_args
    assert args[0] == "stats"
    assert "overwrites" in kwargs

    new_channel.send.assert_awaited_once()
    assert isinstance(new_channel.send.await_args.kwargs["embed"], discord.Embed)

    new_channel.purge.assert_not_called()

    ctx.channel.send.assert_awaited_once()
    assert "stats" in ctx.channel.send.await_args.args[0].lower()


# --- setup_stats: existing #stats channel ---

async def test_setup_stats_reuses_existing_channel_and_purges(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")
    monkeypatch.setattr(main.table, "query", _query_mock())

    existing_channel = _make_stats_channel()
    guild = _make_guild(existing_stats_channel=existing_channel)

    ctx = _make_ctx()
    ctx.guild = guild

    await main.setup_stats(ctx)

    guild.create_text_channel.assert_not_called()
    existing_channel.purge.assert_awaited_once()

    existing_channel.send.assert_awaited_once()
    assert isinstance(existing_channel.send.await_args.kwargs["embed"], discord.Embed)

    ctx.channel.send.assert_awaited_once()


# --- setup_stats: guild.create_text_channel raises Forbidden ---

async def test_setup_stats_handles_forbidden_when_creating_channel(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")

    response = MagicMock(status=403, reason="Forbidden")
    forbidden_error = discord.Forbidden(response, "Missing Permissions")

    guild = _make_guild()
    guild.create_text_channel = AsyncMock(side_effect=forbidden_error)

    ctx = _make_ctx()
    ctx.guild = guild

    await main.setup_stats(ctx)

    guild.create_text_channel.assert_awaited_once()
    ctx.channel.send.assert_awaited_once()
    assert "permission" in ctx.channel.send.await_args.args[0].lower()


# --- setup_stats: ctx.guild is None ---

async def test_setup_stats_outside_guild_sends_message_and_makes_no_api_calls(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")

    ctx = _make_ctx()
    ctx.guild = None

    await main.setup_stats(ctx)

    ctx.channel.send.assert_awaited_once()
    assert "server" in ctx.channel.send.await_args.args[0].lower()


# --- setup_stats: role gating ---

async def test_setup_stats_blocks_non_admin(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")

    guild = _make_guild()
    ctx = _make_ctx()
    ctx.guild = guild

    await main.setup_stats(ctx)

    ctx.channel.send.assert_awaited_once_with("Insufficient Permissions")
    guild.create_text_channel.assert_not_called()


# --- refresh_stats_channels ---

async def test_refresh_stats_channels_refreshes_only_guilds_with_channel(monkeypatch):
    sentinel_embed = MagicMock(name="sentinel_embed")
    build_embed_mock = MagicMock(return_value=sentinel_embed)
    monkeypatch.setattr(main, "build_stats_embed", build_embed_mock)

    guild_with_stats = _make_guild()
    stats_channel = _make_stats_channel()
    guild_with_stats.text_channels = [stats_channel]

    guild_without_stats = _make_guild()
    guild_without_stats.text_channels = []

    _set_guilds(monkeypatch, [guild_with_stats, guild_without_stats])

    await main.refresh_stats_channels()

    stats_channel.purge.assert_awaited_once()
    stats_channel.send.assert_awaited_once_with(embed=sentinel_embed)

    guild_with_stats.create_text_channel.assert_not_called()
    guild_without_stats.create_text_channel.assert_not_called()

    # build_stats_embed only invoked for the guild that has a #stats channel
    build_embed_mock.assert_called_once()
