import importlib
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import MessageId, Update, User

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

    async def test_real_dispatcher_routes_private_updates_and_keyboard(self):
        calls = []
        class LocalSession(BaseSession):
            async def close(self): pass
            async def stream_content(self, *args, **kwargs):
                if False: yield b""
            async def make_request(self, bot, method, timeout=None):
                calls.append(method)
                if method.__api_method__ == "getMe":
                    return User(id=11111, is_bot=True, first_name="Fixture", username="fixture_bot")
                if method.__api_method__ == "copyMessage": return MessageId(message_id=88)
                return True
        bot = Bot("11111:" + "F" * 35, session=LocalSession())
        def msg(who, text, mid, **extra):
            return {"message_id": mid, "date": 1800000000, "chat": {"id": who, "type": "private"},
                    "from": {"id": who, "first_name": "Fixture", "is_bot": False}, "text": text, **extra}
        async def feed(index, **payload):
            return await main.dp.feed_update(bot, Update.model_validate({"update_id": index, **payload}, context={"bot": bot}))
        await feed(1, message=msg(456, "/start", 1))
        self.assertEqual(calls[-1].text, main.DEFAULT_WELCOME)
        await feed(2, message=msg(123, "/start", 2))
        self.assertIn("Вы владелец", calls[-1].text)
        self.assertIsNotNone(calls[-1].reply_markup)
        await feed(3, callback_query={"id": "q", "chat_instance": "c", "from": msg(123, "", 3)["from"],
                                       "message": msg(123, "Настройки", 3), "data": "owner:welcome"})
        await feed(4, message=msg(123, "Спросите про музыку", 4))
        await feed(5, message=msg(456, "/start", 5))
        self.assertEqual(calls[-1].text, "Спросите про музыку")
        await feed(6, message=msg(456, "Вопрос", 6))
        await feed(7, message=msg(123, "Ответ", 7, reply_to_message=msg(123, "Вопрос", 88)))
        routes = [(c.from_chat_id, c.chat_id) for c in calls if c.__api_method__ == "copyMessage"]
        self.assertEqual(routes, [(456, 123), (123, 456)])

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
