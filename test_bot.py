import importlib
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

_data = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = _data.name
os.environ["OWNER_ID"] = "123"
import storage
import main


def message(user=456, text="Вопрос"):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user), chat=SimpleNamespace(id=user, type="private"),
        text=text, message_id=10, answer=AsyncMock(), reply_to_message=None,
    )


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        storage._db.execute("DELETE FROM settings")
        storage._db.execute("DELETE FROM questions")
        storage._db.execute("DELETE FROM banned")
        storage._db.commit()
        main._last_question.clear()

    async def test_guest_cannot_claim_owner_before_owner_start(self):
        guest = message()
        bot = AsyncMock()
        await main.start(guest, bot)
        self.assertEqual(storage.owner_id(), 123)
        self.assertNotIn("Вы владелец", guest.answer.call_args.args[0])
        self.assertFalse(storage.claim_owner(456))
        self.assertTrue(storage.claim_owner(123))

    async def test_question_is_copied_without_forwarding_and_reply_reaches_guest(self):
        guest, bot = message(), AsyncMock()
        bot.copy_message.return_value = SimpleNamespace(message_id=70)
        await main.question(guest, bot)
        bot.copy_message.assert_awaited_once_with(123, 456, 10)
        self.assertEqual(storage.author_of(70), 456)
        self.assertEqual(storage.question_count(), 1)
        owner = message(123, "Ответ")
        owner.reply_to_message = SimpleNamespace(message_id=70)
        await main.answer(owner, bot)
        self.assertEqual(bot.copy_message.call_args.args, (456, 123, 10))

    async def test_rate_limit_and_ban_prevent_repeated_delivery(self):
        guest, bot = message(), AsyncMock()
        bot.copy_message.return_value = SimpleNamespace(message_id=70)
        await main.question(guest, bot)
        await main.question(guest, bot)
        self.assertEqual(bot.copy_message.await_count, 1)
        storage.ban(456)
        main._last_question.clear()
        await main.question(guest, bot)
        self.assertEqual(bot.copy_message.await_count, 1)

    async def test_welcome_editor_persists_without_echoing_as_question(self):
        storage.put("editing_welcome", "1")
        owner = message(123, "Спрашивайте о канале")
        await main.owner_hint(owner)
        self.assertEqual(main.welcome_text(), owner.text)
        self.assertEqual(storage.get("editing_welcome"), "0")
        storage._db.close()
        importlib.reload(storage)
        self.assertEqual(storage.get("welcome"), owner.text)
        self.assertEqual(storage.owner_id(), 123)

    async def test_concurrent_delivery_reserves_one_question(self):
        guest, bot = message(), AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_send(*args):
            entered.set()
            await release.wait()
        bot.send_message.side_effect = slow_send
        bot.copy_message.return_value = SimpleNamespace(message_id=71)
        first = asyncio.create_task(main.question(guest, bot))
        await entered.wait()
        await main.question(guest, bot)
        release.set()
        await first
        self.assertEqual(bot.copy_message.await_count, 1)

    def test_conflicting_or_invalid_owner_fails_closed(self):
        storage.put("owner_id", "999")
        with self.assertRaises(ValueError):
            storage.owner_id()
        storage._db.execute("DELETE FROM settings")
        storage._db.commit()
        os.environ["OWNER_ID"] = "invalid"
        try:
            with self.assertRaises(ValueError):
                storage.owner_id()
        finally:
            os.environ["OWNER_ID"] = "123"


if __name__ == "__main__":
    unittest.main()
