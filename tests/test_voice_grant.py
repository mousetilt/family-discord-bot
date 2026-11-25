import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import discord

from cogs import voice_rooms as vr
from core import config
from core.family import MODULES


def chan(speak, connect, view, own=None, cid=5):
    ch = MagicMock(spec=discord.VoiceChannel)
    ch.id = cid
    ch.overwrites = {} if own is None else {"m": own}
    ch.permissions_for.return_value = NS(speak=speak, connect=connect, view_channel=view)
    return ch


def member(timed_out=False):
    m = MagicMock()
    m.bot = False
    m.guild.afk_channel = None
    m.is_timed_out.return_value = timed_out
    return m


class MovedSpeak(unittest.TestCase):
    """Перенесли в войс, куда человеку нельзя, — он может говорить; сам зашёл в «немой» канал — не трогаем."""

    def test_friend_moved_into_family_voice(self):
        self.assertTrue(vr.needs_grant(member(), chan(False, False, False)))

    def test_joined_listen_only_channel_himself(self):
        self.assertFalse(vr.needs_grant(member(), chan(False, True, True)))

    def test_already_can_speak(self):
        self.assertFalse(vr.needs_grant(member(), chan(True, False, False)))

    def test_afk_and_mute_and_personal_ban_respected(self):
        self.assertFalse(vr.needs_grant(member(), chan(False, False, False, cid=7), afk_id=7))
        self.assertFalse(vr.needs_grant(member(timed_out=True), chan(False, False, False)))
        m = member()
        ch = chan(False, False, False)
        ch.overwrites = {m: discord.PermissionOverwrite(speak=False)}
        self.assertFalse(vr.needs_grant(m, ch))

    def test_friends_voice_and_module(self):
        keys = [k for cat in config.STRUCTURE for _, k, *_ in cat["channels"]]
        self.assertIn("friends_voice", keys)
        self.assertNotIn("friends_voice", config.FRIEND_HIDDEN_VOICES)
        self.assertIn("moved", MODULES["voice"][1])
