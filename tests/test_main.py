"""
Unit tests for the pure/testable logic in main.py.

`main` is imported once in tests/conftest.py, with all required env vars
stubbed and arrapi.SonarrAPI / arrapi.RadarrAPI replaced by dummies before
import, so no network access or real credentials are needed to run these.
"""

from unittest.mock import AsyncMock, MagicMock

import main


# --- get_imdb_id_from_url ---

def test_get_imdb_id_from_url_extracts_id_from_valid_url():
    url = "https://www.imdb.com/title/tt1234567/?ref_=nv_sr_srsg_0"
    assert main.get_imdb_id_from_url(url) == "tt1234567"


def test_get_imdb_id_from_url_returns_none_for_non_imdb_url():
    url = "https://www.example.com/title/tt1234567/"
    assert main.get_imdb_id_from_url(url) is None


def test_get_imdb_id_from_url_returns_none_when_no_title_id_present():
    url = "https://www.imdb.com/name/nm0000001/"
    assert main.get_imdb_id_from_url(url) is None


# --- get_tvdb_id_from_url ---

def test_get_tvdb_id_from_url_extracts_id_from_valid_page(monkeypatch):
    fake_response = MagicMock()
    fake_response.text = '<a class="edit" href="/series/12345/edit">Edit Series</a>'
    fake_response.raise_for_status = MagicMock()
    mock_get = MagicMock(return_value=fake_response)
    monkeypatch.setattr(main.requests, "get", mock_get)

    result = main.get_tvdb_id_from_url("https://thetvdb.com/series/some-show")

    assert result == 12345
    mock_get.assert_called_once_with("https://thetvdb.com/series/some-show", timeout=10)


def test_get_tvdb_id_from_url_returns_none_for_non_tvdb_url_without_requesting(monkeypatch):
    mock_get = MagicMock()
    monkeypatch.setattr(main.requests, "get", mock_get)

    result = main.get_tvdb_id_from_url("https://example.com/series/12345/edit")

    assert result is None
    mock_get.assert_not_called()


def test_get_tvdb_id_from_url_returns_none_when_pattern_missing(monkeypatch):
    fake_response = MagicMock()
    fake_response.text = "<html>no matching pattern here</html>"
    fake_response.raise_for_status = MagicMock()
    monkeypatch.setattr(main.requests, "get", MagicMock(return_value=fake_response))

    result = main.get_tvdb_id_from_url("https://thetvdb.com/series/some-show")

    assert result is None


# --- Role ordering ---

def test_role_ordering():
    assert main.Role.GUEST < main.Role.TRUSTED
    assert main.Role.TRUSTED < main.Role.ADMIN
    assert main.Role.ADMIN < main.Role.OWNER


def test_role_comparisons():
    assert main.Role.ADMIN >= main.Role.TRUSTED
    assert main.Role.TRUSTED >= main.Role.TRUSTED
    assert not (main.Role.GUEST >= main.Role.TRUSTED)
    assert main.Role.OWNER > main.Role.GUEST


# --- require_role decorator ---

def _make_ctx(author_id=123):
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.channel.send = AsyncMock()
    return ctx


async def test_require_role_calls_function_when_role_meets_minimum(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "ADMIN")
    inner = AsyncMock(return_value="handled")

    @main.require_role(main.Role.TRUSTED)
    async def handler(ctx):
        return await inner(ctx)

    ctx = _make_ctx()
    result = await handler(ctx)

    inner.assert_awaited_once_with(ctx)
    ctx.channel.send.assert_not_called()
    assert result == "handled"


async def test_require_role_calls_function_when_role_exactly_meets_minimum(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "TRUSTED")
    inner = AsyncMock(return_value="handled")

    @main.require_role(main.Role.TRUSTED)
    async def handler(ctx):
        return await inner(ctx)

    ctx = _make_ctx()
    await handler(ctx)

    inner.assert_awaited_once_with(ctx)


async def test_require_role_blocks_function_when_role_below_minimum(monkeypatch):
    monkeypatch.setattr(main, "get_user_role", lambda discord_id: "GUEST")
    inner = AsyncMock()

    @main.require_role(main.Role.ADMIN)
    async def handler(ctx):
        return await inner(ctx)

    ctx = _make_ctx()
    result = await handler(ctx)

    inner.assert_not_called()
    ctx.channel.send.assert_awaited_once_with("Insufficient Permissions")
    assert result is None


async def test_require_role_handles_exception_from_get_user_role(monkeypatch):
    def raise_error(discord_id):
        raise RuntimeError("dynamodb is unreachable")

    monkeypatch.setattr(main, "get_user_role", raise_error)
    inner = AsyncMock()

    @main.require_role(main.Role.TRUSTED)
    async def handler(ctx):
        return await inner(ctx)

    ctx = _make_ctx()
    result = await handler(ctx)

    inner.assert_not_called()
    ctx.channel.send.assert_awaited_once_with("Something went wrong checking permissions.")
    assert result is None
