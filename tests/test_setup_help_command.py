"""
Tests for the `!setupHelp` admin command and its supporting `build_help_embed()`
helper (main.py). `setup_help` creates (or refreshes) a #help text channel and
posts a `discord.Embed` documenting every command in `main.COMMAND_DOCS`.

`main` is imported once in tests/conftest.py, with all required env vars
stubbed and arrapi.SonarrAPI / arrapi.RadarrAPI replaced by dummies before
import, so no network access or real credentials are needed to run these.
"""

from unittest.mock import AsyncMock, MagicMock

import discord

import main


def _make_ctx(author_id=123):
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.channel.send = AsyncMock()
    return ctx


def _make_guild(existing_help_channel=None):
    guild = MagicMock()
    guild.text_channels = [existing_help_channel] if existing_help_channel else []
    guild.default_role = "default_role_sentinel"
    guild.me = "guild_me_sentinel"
    guild.create_text_channel = AsyncMock()
    return guild


def _make_help_channel():
    channel = MagicMock()
    channel.name = "help"
    channel.send = AsyncMock()
    channel.purge = AsyncMock()
    channel.mention = "#help"
    return channel


# --- build_help_embed (pure function) ---

def test_build_help_embed_returns_embed():
    embed = main.build_help_embed()
    assert isinstance(embed, discord.Embed)


def test_build_help_embed_has_one_field_per_command_doc():
    embed = main.build_help_embed()
    assert len(embed.fields) == len(main.COMMAND_DOCS)


def test_build_help_embed_field_names_and_role_requirements_match_docs():
    embed = main.build_help_embed()
    for field, doc in zip(embed.fields, main.COMMAND_DOCS):
        assert field.name == doc["usage"]
        assert doc["description"] in field.value
        assert doc["min_role"].name in field.value


# --- setup_help: no existing #help channel ---

async def test_setup_help_creates_channel_when_missing(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")

    new_channel = _make_help_channel()
    guild = _make_guild()
    guild.create_text_channel = AsyncMock(return_value=new_channel)

    ctx = _make_ctx()
    ctx.guild = guild

    await main.setup_help(ctx)

    guild.create_text_channel.assert_awaited_once()
    args, kwargs = guild.create_text_channel.await_args
    assert args[0] == "help"
    assert "overwrites" in kwargs

    new_channel.send.assert_awaited_once()
    assert isinstance(new_channel.send.await_args.kwargs["embed"], discord.Embed)

    new_channel.purge.assert_not_called()

    ctx.channel.send.assert_awaited_once()
    assert "help" in ctx.channel.send.await_args.args[0].lower()


# --- setup_help: existing #help channel ---

async def test_setup_help_reuses_existing_channel_and_purges(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")

    existing_channel = _make_help_channel()
    guild = _make_guild(existing_help_channel=existing_channel)

    ctx = _make_ctx()
    ctx.guild = guild

    await main.setup_help(ctx)

    guild.create_text_channel.assert_not_called()
    existing_channel.purge.assert_awaited_once()

    existing_channel.send.assert_awaited_once()
    assert isinstance(existing_channel.send.await_args.kwargs["embed"], discord.Embed)

    ctx.channel.send.assert_awaited_once()


# --- setup_help: guild.create_text_channel raises Forbidden ---

async def test_setup_help_handles_forbidden_when_creating_channel(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")

    response = MagicMock(status=403, reason="Forbidden")
    forbidden_error = discord.Forbidden(response, "Missing Permissions")

    guild = _make_guild()
    guild.create_text_channel = AsyncMock(side_effect=forbidden_error)

    ctx = _make_ctx()
    ctx.guild = guild

    await main.setup_help(ctx)

    guild.create_text_channel.assert_awaited_once()
    ctx.channel.send.assert_awaited_once()
    assert "permission" in ctx.channel.send.await_args.args[0].lower()


# --- setup_help: ctx.guild is None ---

async def test_setup_help_outside_guild_sends_message_and_makes_no_api_calls(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")

    ctx = _make_ctx()
    ctx.guild = None

    await main.setup_help(ctx)

    ctx.channel.send.assert_awaited_once()
    assert "server" in ctx.channel.send.await_args.args[0].lower()


# --- setup_help: role gating ---

async def test_setup_help_blocks_non_admin(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")

    guild = _make_guild()
    ctx = _make_ctx()
    ctx.guild = guild

    await main.setup_help(ctx)

    ctx.channel.send.assert_awaited_once_with("Insufficient Permissions")
    guild.create_text_channel.assert_not_called()
