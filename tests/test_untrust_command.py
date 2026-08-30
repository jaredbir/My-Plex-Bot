"""
Tests for the `!untrust` command (main.py), which reverts any mentioned user
who is currently Trusted back to Guest. Users at Guest, Admin, or Owner are
left untouched -- not demoted, and not mentioned in either confirmation
message, since the command sends exactly one message (an if/else, not two
independent ifs like `makeAdmin`).

`main` is imported once in tests/conftest.py, with all required env vars
stubbed and arrapi.SonarrAPI / arrapi.RadarrAPI replaced by dummies before
import, so no network access or real credentials are needed to run these.
"""

from unittest.mock import AsyncMock, MagicMock

import main


def _make_ctx(author_id=123, mentions=None):
    ctx = MagicMock()
    ctx.author.id = author_id
    ctx.channel.send = AsyncMock()
    ctx.message.mentions = mentions or []
    return ctx


def _make_member(member_id, mention):
    member = MagicMock()
    member.id = member_id
    member.mention = mention
    return member


def _role_map_getter(role_map):
    return lambda discord_id: role_map.get(discord_id, "GUEST")


async def test_untrust_demotes_single_trusted_user(monkeypatch):
    member = _make_member(456, "<@456>")
    role_map = {123: "ADMIN", 456: "TRUSTED"}
    monkeypatch.setattr(main, "get_user_role", _role_map_getter(role_map))
    set_role = MagicMock()
    monkeypatch.setattr(main, "set_role", set_role)

    ctx = _make_ctx(mentions=[member])
    await main.untrust(ctx)

    set_role.assert_called_once_with(456, main.Role.GUEST)
    ctx.channel.send.assert_awaited_once_with("<@456> are no longer Trusted")


async def test_untrust_leaves_guest_user_alone(monkeypatch):
    member = _make_member(456, "<@456>")
    role_map = {123: "ADMIN", 456: "GUEST"}
    monkeypatch.setattr(main, "get_user_role", _role_map_getter(role_map))
    set_role = MagicMock()
    monkeypatch.setattr(main, "set_role", set_role)

    ctx = _make_ctx(mentions=[member])
    await main.untrust(ctx)

    set_role.assert_not_called()
    ctx.channel.send.assert_awaited_once_with("None of the mentioned users were Trusted")


async def test_untrust_leaves_admin_user_alone(monkeypatch):
    member = _make_member(456, "<@456>")
    role_map = {123: "ADMIN", 456: "ADMIN"}
    monkeypatch.setattr(main, "get_user_role", _role_map_getter(role_map))
    set_role = MagicMock()
    monkeypatch.setattr(main, "set_role", set_role)

    ctx = _make_ctx(mentions=[member])
    await main.untrust(ctx)

    set_role.assert_not_called()
    ctx.channel.send.assert_awaited_once_with("None of the mentioned users were Trusted")


async def test_untrust_leaves_owner_user_alone(monkeypatch):
    member = _make_member(456, "<@456>")
    role_map = {123: "ADMIN", 456: "OWNER"}
    monkeypatch.setattr(main, "get_user_role", _role_map_getter(role_map))
    set_role = MagicMock()
    monkeypatch.setattr(main, "set_role", set_role)

    ctx = _make_ctx(mentions=[member])
    await main.untrust(ctx)

    set_role.assert_not_called()
    ctx.channel.send.assert_awaited_once_with("None of the mentioned users were Trusted")


async def test_untrust_mixed_mentions_only_demotes_trusted_ones(monkeypatch):
    trusted_member = _make_member(456, "<@456>")
    guest_member = _make_member(789, "<@789>")
    admin_member = _make_member(111, "<@111>")
    another_trusted_member = _make_member(222, "<@222>")

    role_map = {
        123: "ADMIN",
        456: "TRUSTED",
        789: "GUEST",
        111: "ADMIN",
        222: "TRUSTED",
    }
    monkeypatch.setattr(main, "get_user_role", _role_map_getter(role_map))
    set_role = MagicMock()
    monkeypatch.setattr(main, "set_role", set_role)

    ctx = _make_ctx(mentions=[trusted_member, guest_member, admin_member, another_trusted_member])
    await main.untrust(ctx)

    assert set_role.call_count == 2
    set_role.assert_any_call(456, main.Role.GUEST)
    set_role.assert_any_call(222, main.Role.GUEST)

    sent_text = ctx.channel.send.await_args.args[0]
    assert "<@456>" in sent_text
    assert "<@222>" in sent_text
    assert "<@789>" not in sent_text
    assert "<@111>" not in sent_text
    ctx.channel.send.assert_awaited_once()


async def test_untrust_requires_admin_role(monkeypatch):
    member = _make_member(456, "<@456>")
    role_map = {123: "TRUSTED", 456: "TRUSTED"}
    monkeypatch.setattr(main, "get_user_role", _role_map_getter(role_map))
    set_role = MagicMock()
    monkeypatch.setattr(main, "set_role", set_role)

    ctx = _make_ctx(mentions=[member])
    await main.untrust(ctx)

    set_role.assert_not_called()
    ctx.channel.send.assert_awaited_once_with("Insufficient Permissions")
