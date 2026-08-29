"""
Tests for the "already in the library" early-return behavior in
`requestMovie` and `requestTV` (main.py).

Both commands ask Radarr/Sonarr whether the requested title already has a
file (`movie.hasFile` / `series.episodeFileCount`). When it does, the bot
tells the user and returns *before* writing anything to DynamoDB, which
means the downstream Lambda (driven by DynamoDB Streams) never sees a new
request and never re-adds anything. These tests prove that behavior stays
in place -- both that it fires when it should, and that it does NOT fire
(and a request record IS written) on the happy path.

`main` is imported once in tests/conftest.py, with all required env vars
stubbed and arrapi.SonarrAPI / arrapi.RadarrAPI replaced by dummies before
import, so no network access or real credentials are needed to run these.
"""

from unittest.mock import AsyncMock, MagicMock

import main


def _make_ctx(author_id=123):
    ctx = MagicMock()
    ctx.author.id = author_id

    sent_message = MagicMock()
    sent_message.id = 999
    sent_message.add_reaction = AsyncMock()

    ctx.channel.send = AsyncMock(return_value=sent_message)
    return ctx


# --- requestMovie: already in library ---

async def test_request_movie_already_in_library_does_not_write_request(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    monkeypatch.setattr(main.table, "get_item", MagicMock(return_value={}))
    put_item = MagicMock()
    monkeypatch.setattr(main.table, "put_item", put_item)

    fake_movie = MagicMock()
    fake_movie.hasFile = True
    fake_movie.title = "Some Movie"
    monkeypatch.setattr(main.radarr, "get_movie", MagicMock(return_value=fake_movie))

    ctx = _make_ctx()

    await main.requestMovie(ctx, "https://www.imdb.com/title/tt0000001/")

    sent_texts = [call.args[0] for call in ctx.channel.send.await_args_list]
    assert any("already in the library" in text for text in sent_texts)
    put_item.assert_not_called()


async def test_request_movie_not_in_library_writes_request(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    monkeypatch.setattr(main.table, "get_item", MagicMock(return_value={}))
    put_item = MagicMock()
    monkeypatch.setattr(main.table, "put_item", put_item)

    fake_movie = MagicMock()
    fake_movie.hasFile = False
    fake_movie.title = "Some Movie"
    monkeypatch.setattr(main.radarr, "get_movie", MagicMock(return_value=fake_movie))

    ctx = _make_ctx()

    await main.requestMovie(ctx, "https://www.imdb.com/title/tt0000001/")

    sent_texts = [call.args[0] for call in ctx.channel.send.await_args_list]
    assert not any("already in the library" in text for text in sent_texts)
    put_item.assert_called()


# --- requestTV: already in library ---

async def test_request_tv_already_in_library_does_not_write_request(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    monkeypatch.setattr(main, "get_tvdb_id_from_url", lambda url: 12345)
    monkeypatch.setattr(main.table, "get_item", MagicMock(return_value={}))
    put_item = MagicMock()
    monkeypatch.setattr(main.table, "put_item", put_item)

    fake_series = MagicMock()
    fake_series.episodeFileCount = 3
    fake_series.title = "Some Show"
    monkeypatch.setattr(main.sonarr, "get_series", MagicMock(return_value=fake_series))

    ctx = _make_ctx()

    await main.requestTV(ctx, "https://thetvdb.com/series/some-show")

    sent_texts = [call.args[0] for call in ctx.channel.send.await_args_list]
    assert any("already in the library" in text for text in sent_texts)
    put_item.assert_not_called()


async def test_request_tv_not_in_library_writes_request(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    monkeypatch.setattr(main, "get_tvdb_id_from_url", lambda url: 12345)
    monkeypatch.setattr(main.table, "get_item", MagicMock(return_value={}))
    put_item = MagicMock()
    monkeypatch.setattr(main.table, "put_item", put_item)

    fake_series = MagicMock()
    fake_series.episodeFileCount = 0
    fake_series.title = "Some Show"
    monkeypatch.setattr(main.sonarr, "get_series", MagicMock(return_value=fake_series))

    ctx = _make_ctx()

    await main.requestTV(ctx, "https://thetvdb.com/series/some-show")

    sent_texts = [call.args[0] for call in ctx.channel.send.await_args_list]
    assert not any("already in the library" in text for text in sent_texts)
    put_item.assert_called()
