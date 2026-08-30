"""
Tests for `log_request_event` (main.py), the best-effort helper that posts
a message to the #request-log channel, creating the channel if it doesn't
exist yet.

This helper is deliberately "logging-only" -- any failure inside it must
never propagate to the caller (the approval/denial flow, or the request
flow), so it wraps its body in a broad try/except that just prints and
returns. These tests cover: no-op when there's no guild, reusing an
existing channel, creating the channel when missing, and swallowing errors.

`main` is imported once in tests/conftest.py, with all required env vars
stubbed and arrapi.SonarrAPI / arrapi.RadarrAPI replaced by dummies before
import, so no network access or real credentials are needed to run these.
"""

from unittest.mock import AsyncMock, MagicMock

import main


async def test_log_request_event_no_guild_is_a_noop():
    # guild=None must return immediately without raising or touching anything.
    await main.log_request_event(None, "some message")


async def test_log_request_event_uses_existing_channel():
    existing_channel = MagicMock()
    existing_channel.name = 'request-log'
    existing_channel.send = AsyncMock()

    guild = MagicMock()
    guild.text_channels = [existing_channel]
    guild.create_text_channel = AsyncMock()

    await main.log_request_event(guild, "some message")

    guild.create_text_channel.assert_not_called()
    existing_channel.send.assert_awaited_once_with("some message")


async def test_log_request_event_creates_channel_when_missing():
    new_channel = MagicMock()
    new_channel.send = AsyncMock()

    guild = MagicMock()
    guild.text_channels = []
    guild.create_text_channel = AsyncMock(return_value=new_channel)
    guild.default_role = "sentinel-default-role"
    guild.me = "sentinel-me"

    await main.log_request_event(guild, "some message")

    guild.create_text_channel.assert_awaited_once()
    args, kwargs = guild.create_text_channel.await_args
    # channel name is either the first positional arg or a 'name' kwarg
    channel_name = args[0] if args else kwargs.get('name')
    assert channel_name == 'request-log'
    new_channel.send.assert_awaited_once_with("some message")


async def test_log_request_event_swallows_errors():
    guild = MagicMock()
    guild.text_channels = []
    guild.create_text_channel = AsyncMock(side_effect=RuntimeError("boom"))

    # Must not raise -- logging failures must never crash the caller.
    await main.log_request_event(guild, "some message")
