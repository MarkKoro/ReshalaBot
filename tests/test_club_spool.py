import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123:test")
import bot


def _update(chat_id, update_id=7, edited=False):
    post = SimpleNamespace(chat=SimpleNamespace(id=chat_id))
    return SimpleNamespace(update_id=update_id, channel_post=None if edited else post,
                           edited_channel_post=post if edited else None,
                           to_dict=lambda: {"update_id": update_id, "channel_post": {"message_id": 1}})


class ClubSpoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_post_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "CLUB_SPOOL_DIR", d):
            await bot.club_feed_spool(_update(bot.CHANNEL_ID), None)
            await bot.club_feed_spool(_update(bot.CHANNEL_ID, 8, edited=True), None)
            names = sorted(os.listdir(d))
            self.assertEqual(names, ["%020d.json" % 7, "%020d.json" % 8])
            with open(os.path.join(d, names[0]), encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["update_id"], 7)

    async def test_other_chats_and_missing_dir_are_ignored(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "CLUB_SPOOL_DIR", d):
            await bot.club_feed_spool(_update(bot.CHANNEL_ID - 1), None)
            self.assertEqual(os.listdir(d), [])
        with mock.patch.object(bot, "CLUB_SPOOL_DIR", "/nonexistent/club-spool"):
            await bot.club_feed_spool(_update(bot.CHANNEL_ID), None)

    async def test_write_error_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bot, "CLUB_SPOOL_DIR", d),                 mock.patch("builtins.open", side_effect=OSError("disk full")):
            await bot.club_feed_spool(_update(bot.CHANNEL_ID), None)


if __name__ == "__main__":
    unittest.main()
