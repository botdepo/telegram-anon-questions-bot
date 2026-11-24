import importlib
import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.filters import CommandObject
from aiogram.types import Message, MessageId, Update, User

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


def reset_storage():
    for table in ("settings", "questions", "banned", "guest_collections", "collections"):
        storage._db.execute(f"DELETE FROM {table}")
    storage._db.commit()
    (storage.DATA_DIR / ".first-answer-delivered").unlink(missing_ok=True)
    main._last_question.clear()


def telegram_message(who, text, mid, **extra):
    return {
        "message_id": mid, "date": 1800000000,
        "chat": {"id": who, "type": "private"},
        "from": {"id": who, "first_name": "Fixture", "is_bot": False},
        "text": text, **extra,
    }


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.deliveries = []
        self.next_message_id = 1000

    async def close(self): pass

    async def stream_content(self, *args, **kwargs):
        if False: yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        api = method.__api_method__
        if api == "getMe":
            return User(id=11111, is_bot=True, first_name="Fixture", username="fixture_bot")
        if api in ("copyMessage", "sendMessage"):
            self.next_message_id += 1
            if api == "copyMessage":
                result = MessageId(message_id=self.next_message_id)
            else:
                result = Message.model_validate(
                    telegram_message(method.chat_id, method.text, self.next_message_id),
                    context={"bot": bot},
                )
            self.deliveries.append((method, result))
            return result
        return True


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_storage()

    async def test_guest_cannot_claim_owner_before_owner_start(self):
        guest = message()
        bot = AsyncMock()
        await main.start(guest, bot, CommandObject(command="start"))
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
        with unittest.mock.patch.dict(os.environ, {"BOTDEPO_MEASURE_REPLY": "1"}):
            flag = storage.DATA_DIR / ".first-answer-delivered"
            from aiogram.exceptions import TelegramBadRequest
            bot.copy_message.side_effect = TelegramBadRequest(method=None, message="blocked")
            await main.answer(owner, bot)
            self.assertFalse(flag.exists())
            bot.copy_message.side_effect = None
            await main.answer(owner, bot)
            self.assertEqual(flag.read_bytes(), b"")
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
        await main.owner_hint(owner, AsyncMock())
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


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset_storage()
        self.session = RecordingSession()
        self.bot = Bot("11111:" + "F" * 35, session=self.session)
        self.update_id = 0

    async def asyncTearDown(self):
        await self.bot.session.close()

    async def feed(self, **payload):
        self.update_id += 1
        await main.dp.feed_update(
            self.bot,
            Update.model_validate({"update_id": self.update_id, **payload}, context={"bot": self.bot}),
        )

    async def send(self, who, text, **extra):
        await self.feed(message=telegram_message(who, text, self.update_id + 1, **extra))

    async def click(self, who, data, chat_type="private"):
        await self.feed(callback_query={
            "id": str(self.update_id + 1), "chat_instance": "fixture",
            "from": telegram_message(who, "", 1)["from"],
            "message": telegram_message(who, "Меню", 1, chat={"id": who, "type": chat_type}),
            "data": data,
        })

    def last_text(self):
        return next(c.text for c in reversed(self.session.calls) if c.__api_method__ == "sendMessage")

    async def test_dispatcher_collection_link_media_question_and_reply(self):
        await self.send(123, "/start")
        self.assertIn("Новый сбор", str(self.session.calls[-1].reply_markup))
        await self.click(123, "owner:new_collection")
        await self.send(123, "  Поиск\nработы  ")
        collection = storage.collections_page(0, 1)[0]
        self.assertEqual(collection.title, "Поиск работы")
        link = f"https://t.me/fixture_bot?start=topic_{collection.id}"
        self.assertIn(link, self.last_text())
        self.assertRegex(collection.id, r"^[A-Za-z0-9_-]{12}$")
        self.assertLessEqual(len(f"topic_{collection.id}"), 64)

        await self.send(456, f"/start topic_{collection.id}")
        self.assertIn("Сбор вопросов: Поиск работы", self.last_text())
        await self.send(456, None, photo=[{"file_id": "photo", "file_unique_id": "unique", "width": 20, "height": 20}])
        copied, result = next(
            (call, result) for call, result in self.session.deliveries if call.__api_method__ == "copyMessage"
        )
        self.assertEqual((copied.from_chat_id, copied.chat_id), (456, 123))
        header = next(
            call for call, sent in self.session.deliveries if sent.message_id == copied.reply_parameters.message_id
        )
        self.assertEqual(header.text, "Новый анонимный вопрос\nСбор: Поиск работы")
        self.assertEqual(storage.question_count(collection.id), 1)
        self.assertEqual(storage.author_of(result.message_id), 456)
        await self.send(123, "Начните с портфолио", reply_to_message=telegram_message(123, None, result.message_id))
        replies = [c for c in self.session.calls if c.__api_method__ == "copyMessage"]
        self.assertEqual((replies[-1].from_chat_id, replies[-1].chat_id), (123, 456))
        self.assertFalse(any(c.__api_method__ == "forwardMessage" for c in self.session.calls))
        await self.click(123, f"owner:collection:{collection.id}")
        self.assertIn("Получено вопросов: 1", self.last_text())
        self.assertIn(link, self.last_text())

    async def test_guest_contexts_and_counts_survive_reopening_database(self):
        first = storage.create_collection("Поиск работы")
        second = storage.create_collection("Портфолио")
        await self.send(456, f"/start topic_{first.id}")
        await self.send(789, f"/start topic_{second.id}")
        await self.send(456, "Как начать?")
        storage._db.close()
        importlib.reload(storage)
        self.assertEqual(storage.selected_collection(456), first)
        self.assertEqual(storage.selected_collection(789), second)
        self.assertEqual(storage.collection_by_id(first.id), first)
        await self.send(789, "Что показать?")
        self.assertEqual(storage.question_count(first.id), 1)
        self.assertEqual(storage.question_count(second.id), 1)
        main._last_question.clear()
        await self.send(456, f"/start topic_{second.id}")
        await self.send(456, "Можно без опыта?")
        self.assertEqual(storage.question_count(first.id), 1)
        self.assertEqual(storage.question_count(second.id), 2)

    async def test_general_start_invalid_links_and_button_clear_old_topic(self):
        collection = storage.create_collection("Тема")
        for action in ("/start", "/start general", "/start topic_missing", "/start broken_payload", "guest:general"):
            with self.subTest(action=action):
                await self.send(456, f"/start topic_{collection.id}")
                if action == "guest:general":
                    await self.click(456, action)
                else:
                    await self.send(456, action)
                self.assertIsNone(storage.selected_collection(456))
                if action not in ("/start", "/start general"):
                    self.assertIn("общий вопрос", self.last_text())
                main._last_question.clear()
                await self.send(456, "Общий вопрос")
        self.assertEqual(storage.question_count(collection.id), 0)
        self.assertEqual(storage.question_count(), 5)
        copies = [c for c in self.session.calls if c.__api_method__ == "copyMessage"]
        self.assertTrue(all(c.reply_parameters is None for c in copies))

    async def test_guests_and_group_callbacks_cannot_manage_collections(self):
        storage.put("creating_collection", "1")
        for who, chat_type in ((456, "private"), (123, "group")):
            for action in ("owner:new_collection", "owner:collections:0", "owner:collection:missing", "owner:welcome"):
                await self.click(who, action, chat_type)
        self.assertEqual(storage.get("creating_collection"), "1")
        self.assertIsNone(storage.get("editing_welcome"))
        self.assertEqual(storage.collections_page(0, 10), [])
        self.assertTrue(all(c.__api_method__ == "answerCallbackQuery" for c in self.session.calls))
        await self.send(456, "Тема гостя")
        self.assertEqual(storage.collections_page(0, 10), [])
        self.assertEqual(storage.question_count(), 1)

    async def test_title_validation_cancellation_and_editor_switching(self):
        await self.click(123, "owner:new_collection")
        for title in (" ", "я" * 101, None):
            await self.send(123, title, **({"sticker": {
                "file_id": "sticker", "file_unique_id": "unique", "type": "regular",
                "width": 20, "height": 20, "is_animated": False, "is_video": False,
            }} if title is None else {}))
            self.assertEqual(storage.collections_page(0, 10), [])
            self.assertEqual(storage.get("creating_collection"), "1")
        await self.send(123, "/cancel")
        await self.send(123, "Не создавайте сбор")
        self.assertEqual(storage.collections_page(0, 10), [])
        await self.click(123, "owner:new_collection")
        await self.send(123, "я" * 100, reply_to_message=telegram_message(123, "Название сбора?", 999))
        self.assertEqual(len(storage.collections_page(0, 10)), 1)
        await self.click(123, "owner:new_collection")
        await self.click(123, "owner:welcome")
        await self.send(123, "Общее приветствие")
        self.assertEqual(main.welcome_text(), "Общее приветствие")
        self.assertEqual(len(storage.collections_page(0, 10)), 1)
        await self.click(123, "owner:new_collection")
        await self.click(123, "owner:collections:0")
        await self.send(123, "Не название")
        self.assertEqual(len(storage.collections_page(0, 10)), 1)
        await self.click(123, "owner:new_collection")
        await self.send(123, "/start")
        self.assertEqual(storage.get("creating_collection"), "0")

    async def test_collection_list_pages_keep_older_links_accessible(self):
        await self.click(123, "owner:collections:0")
        self.assertIn("пока нет сборов", self.last_text())
        collections = [storage.create_collection(f"Тема {i}") for i in range(6)]
        storage.remember_question(77, 456, collections[0].id)
        await self.click(123, "owner:collections:0")
        keyboard = self.session.calls[-1].reply_markup.inline_keyboard
        callbacks = [b.callback_data for row in keyboard for b in row]
        self.assertIn("owner:collections:5", callbacks)
        self.assertNotIn(f"owner:collection:{collections[0].id}", callbacks)
        await self.click(123, "owner:collections:5")
        keyboard = self.session.calls[-1].reply_markup.inline_keyboard
        self.assertEqual(keyboard[0][0].text, "Тема 0 · 1")
        self.assertIn("owner:collections:0", [b.callback_data for row in keyboard for b in row])
        await self.click(123, keyboard[0][0].callback_data)
        expected = f"https://t.me/fixture_bot?start=topic_{collections[0].id}"
        self.assertIn(expected, self.last_text())
        await self.send(123, f"/start topic_{collections[0].id}")
        self.assertIn(expected, self.last_text())
        self.assertIsNone(storage.selected_collection(123))
        await self.click(123, "owner:link")
        self.assertIn("https://t.me/fixture_bot?start=general", self.last_text())

    async def test_general_button_clears_topic_before_network_acknowledgement(self):
        collection = storage.create_collection("Тема")
        storage.select_collection(456, collection.id)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_ack():
            entered.set()
            await release.wait()
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=456), message=message(),
            answer=AsyncMock(side_effect=slow_ack),
        )
        task = asyncio.create_task(main.general_question(query))
        await entered.wait()
        try:
            await self.send(456, "Уже общий вопрос")
            self.assertEqual(storage.question_count(), 1)
            self.assertEqual(storage.question_count(collection.id), 0)
        finally:
            release.set()
            await task

    async def test_creation_mode_is_ready_while_callback_acknowledgement_is_pending(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_ack():
            entered.set()
            await release.wait()
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=123), message=message(123),
            data="owner:new_collection", answer=AsyncMock(side_effect=slow_ack),
        )
        task = asyncio.create_task(main.owner_button(query, self.bot))
        await entered.wait()
        try:
            await self.send(123, "Быстрый ввод")
            self.assertEqual(storage.collections_page(0, 1)[0].title, "Быстрый ввод")
            self.assertEqual(storage.get("creating_collection"), "0")
        finally:
            release.set()
            await task
        query.message.answer.assert_not_awaited()

    async def test_in_flight_question_retains_topic_when_guest_switches_link(self):
        first = storage.create_collection("Первая")
        second = storage.create_collection("Вторая")
        storage.select_collection(456, first.id)
        bot = AsyncMock()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_notice(*args):
            entered.set()
            await release.wait()
            return SimpleNamespace(message_id=80)
        bot.send_message.side_effect = slow_notice
        bot.copy_message.return_value = SimpleNamespace(message_id=81)
        task = asyncio.create_task(main.question(message(), bot))
        await entered.wait()
        await self.send(456, f"/start topic_{second.id}")
        release.set()
        await task
        self.assertEqual(storage.selected_collection(456), second)
        self.assertEqual(storage.question_count(first.id), 1)
        self.assertEqual(storage.question_count(second.id), 0)
        self.assertIn("Первая", bot.send_message.call_args.args[1])
        self.assertEqual(bot.copy_message.call_args.kwargs["reply_parameters"].message_id, 80)

    async def test_failed_delivery_does_not_count_and_can_be_retried(self):
        from aiogram.exceptions import TelegramBadRequest
        collection = storage.create_collection("Тема")
        storage.select_collection(456, collection.id)
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(message_id=80)
        bot.copy_message.side_effect = TelegramBadRequest(method=None, message="not delivered")
        guest = message()
        await main.question(guest, bot)
        self.assertEqual(storage.question_count(collection.id), 0)
        self.assertNotIn(456, main._last_question)
        bot.copy_message.side_effect = None
        bot.copy_message.return_value = SimpleNamespace(message_id=81)
        await main.question(guest, bot)
        self.assertEqual(storage.question_count(collection.id), 1)

    async def test_switching_links_does_not_bypass_rate_limit_or_ban(self):
        first = storage.create_collection("Первая")
        second = storage.create_collection("Вторая")
        await self.send(456, f"/start topic_{first.id}")
        await self.send(456, "Первый")
        await self.send(456, f"/start topic_{second.id}")
        await self.send(456, "Повтор")
        self.assertEqual(storage.question_count(second.id), 0)
        self.assertIn("10 секунд", self.last_text())
        storage.ban(456)
        main._last_question.clear()
        await self.send(456, "/start")
        await self.send(456, "После блокировки")
        self.assertEqual(storage.question_count(), 1)

    def test_old_database_upgrade_preserves_routes_settings_and_bans(self):
        with tempfile.TemporaryDirectory() as old_data:
            old = sqlite3.connect(Path(old_data) / "questions.db")
            old.executescript("""
                CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE questions (
                    owner_message_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE banned (user_id INTEGER PRIMARY KEY);
                INSERT INTO settings VALUES ('owner_id', '123'), ('welcome', 'Привет из старой версии');
                INSERT INTO questions VALUES (44, 456, '2026-11-24 12:00:00');
                INSERT INTO banned VALUES (789);
            """)
            old.close()
            storage._db.close()
            try:
                with patch.dict(os.environ, {"DATA_DIR": old_data}):
                    importlib.reload(storage)
                    self.assertEqual(storage.owner_id(), 123)
                    self.assertEqual(main.welcome_text(), "Привет из старой версии")
                    self.assertEqual(storage.author_of(44), 456)
                    self.assertEqual(storage.question_count(), 1)
                    self.assertTrue(storage.is_banned(789))
                    self.assertEqual(storage._db.execute(
                        "SELECT created_at, collection_id FROM questions WHERE owner_message_id = 44",
                    ).fetchone(), ("2026-11-24 12:00:00", None))
                    collection = storage.create_collection("Новый сбор")
                    storage.remember_question(45, 456, collection.id)
                    storage._db.close()
                    importlib.reload(storage)
                    self.assertEqual(storage.question_count(), 2)
                    self.assertEqual(storage.question_count(collection.id), 1)
                    self.assertEqual(storage.author_of(44), 456)
            finally:
                storage._db.close()
                importlib.reload(storage)


if __name__ == "__main__":
    unittest.main()
