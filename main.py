"""Бот анонимных вопросов.

Кто угодно пишет боту — владелец получает вопрос без имени автора и
отвечает обычным «Ответить» в Telegram; бот пересылает ответ автору.
Автор так и остаётся анонимным: бот никому не показывает, кто спросил.

Сервис задаёт владельца через OWNER_ID до публикации ссылки.

Токен бот получает из переменной окружения BOT_TOKEN — на botdepo её
подставляет панель, локально задайте сами (см. README).
"""
import asyncio
import logging
import os
import re
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TEST
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters

import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

dp = Dispatcher()

DEFAULT_WELCOME = (
    "Привет! Напишите свой вопрос — он придёт владельцу без имени отправителя. "
    "Когда ответят, ответ придёт сюда."
)
MAX_WELCOME = 2000
COLLECTIONS_PER_PAGE = 5
_last_question: dict[int, float] = {}


def owner_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Новый сбор", callback_data="owner:new_collection"),
         InlineKeyboardButton(text="Мои сборы", callback_data="owner:collections:0")],
        [InlineKeyboardButton(text="Общая ссылка", callback_data="owner:link"),
         InlineKeyboardButton(text="Приветствие", callback_data="owner:welcome")],
        [InlineKeyboardButton(text="Статистика", callback_data="owner:stats"),
         InlineKeyboardButton(text="Как отвечать", callback_data="owner:help")],
    ])


def general_question_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Задать общий вопрос", callback_data="guest:general")],
    ])


def collection_invitation(collection: storage.Collection) -> str:
    return (
        f"Сбор вопросов: {collection.title}\n\n"
        "Напишите вопрос по этой теме — он придёт владельцу без имени отправителя. "
        "Ответ придёт сюда.\n\n"
        "Следующие сообщения тоже относятся к этому сбору. "
        "Для другой темы откройте её ссылку, для вопроса без темы — нажмите кнопку ниже."
    )


async def show_collection(message: Message, bot: Bot, collection: storage.Collection) -> None:
    me = await bot.get_me()
    await message.answer(
        f"Сбор: {collection.title}\n"
        f"Получено вопросов: {storage.question_count(collection.id)}\n\n"
        f"https://t.me/{me.username}?start=topic_{collection.id}\n\n"
        "Поставьте эту ссылку под постом. Читатель увидит тему, "
        "а его вопрос придёт вам с названием сбора.\n"
        "Чтобы проверить приглашение, откройте ссылку со второго аккаунта.",
        reply_markup=owner_menu(),
    )


async def show_collections(message: Message, offset: int) -> None:
    page = storage.collections_page(offset, COLLECTIONS_PER_PAGE + 1)
    buttons = [
        [InlineKeyboardButton(
            text=f"{collection.title[:45]}{'…' if len(collection.title) > 45 else ''} · {storage.question_count(collection.id)}",
            callback_data=f"owner:collection:{collection.id}",
        )]
        for collection in page[:COLLECTIONS_PER_PAGE]
    ]
    navigation = []
    if offset:
        navigation.append(InlineKeyboardButton(
            text="← Назад", callback_data=f"owner:collections:{max(0, offset - COLLECTIONS_PER_PAGE)}",
        ))
    if len(page) > COLLECTIONS_PER_PAGE:
        navigation.append(InlineKeyboardButton(
            text="Далее →", callback_data=f"owner:collections:{offset + COLLECTIONS_PER_PAGE}",
        ))
    if navigation:
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton(text="Новый сбор", callback_data="owner:new_collection")])
    buttons.append([InlineKeyboardButton(text="Меню", callback_data="owner:menu")])
    await message.answer(
        "Мои сборы\n\nЧисло рядом с темой — полученные вопросы. Нажмите на тему, чтобы получить ссылку."
        if page else "Здесь пока нет сборов. Создайте первый, например «Вопросы про поиск работы».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("owner:"))
async def owner_button(query: CallbackQuery, bot: Bot) -> None:
    if not query.message or query.message.chat.type != "private" or query.from_user.id != storage.owner_id():
        await query.answer()
        return
    action = query.data.split(":", 1)[1]
    # Set the input mode before a network await; the next message can arrive immediately.
    storage.put("editing_welcome", "1" if action == "welcome" else "0")
    storage.put("creating_collection", "1" if action == "new_collection" else "0")
    await query.answer()
    if action == "link":
        me = await bot.get_me()
        await query.message.answer(
            f"Общая ссылка:\nhttps://t.me/{me.username}?start=general\n\n"
            "Поставьте её в профиль или описание канала. Вопросы по этой ссылке приходят без темы.",
            reply_markup=owner_menu(),
        )
    elif action == "stats":
        await query.message.answer(f"Получено вопросов: {storage.question_count()}.", reply_markup=owner_menu())
    elif action == "welcome" and storage.get("editing_welcome") == "1":
        await query.message.answer(
            f"Пришлите новое приветствие одним текстовым сообщением, до {MAX_WELCOME} символов.\n\n"
            f"Сейчас:\n{welcome_text()}\n\n/cancel — оставить как есть."
        )
    elif action == "new_collection" and storage.get("creating_collection") == "1":
        await query.message.answer(
            "О чём собираем вопросы?\n\n"
            f"Пришлите название до {storage.MAX_COLLECTION_TITLE} символов, например «Поиск работы». "
            "Оно будет видно читателю и рядом с его вопросом.\n\n"
            "/cancel — отменить.",
        )
    elif re.fullmatch(r"collections:[0-9]{1,9}", action):
        await show_collections(query.message, int(action.split(":")[1]))
    elif action.startswith("collection:"):
        collection = storage.collection_by_id(action.split(":", 1)[1])
        if collection:
            await show_collection(query.message, bot, collection)
        else:
            await query.message.answer("Сбор не найден. Откройте «Мои сборы».", reply_markup=owner_menu())
    elif action == "menu":
        await query.message.answer("Выберите действие:", reply_markup=owner_menu())
    elif action == "help":
        await query.message.answer(
            "Нажмите на сообщение с вопросом → «Ответить» и напишите ответ. Можно фото или голосовое.\n\n"
            "/ban ответом на вопрос — заблокировать автора.\n/unban — снять все блокировки.\n\n"
            "«Новый сбор» — отдельная ссылка для вопросов к посту или теме. "
            "«Мои сборы» — ссылки и число вопросов по каждой теме.\n\n"
            "Для проверки попросите друга задать вопрос и убедитесь, что ответ пришёл ему.",
            reply_markup=owner_menu(),
        )


def welcome_text() -> str:
    return storage.get("welcome") or DEFAULT_WELCOME


async def is_owner(event: Message | CallbackQuery) -> bool:
    # async, а не F.func: синхронные фильтры aiogram гоняет в другом потоке, а SQLite так не умеет
    chat = event.chat if isinstance(event, Message) else event.message.chat if event.message else None
    return chat is not None and chat.type == "private" and event.from_user.id == storage.owner_id()


@dp.message(CommandStart())
async def start(message: Message, bot: Bot, command: CommandObject) -> None:
    if message.chat.type != "private":
        return
    if storage.claim_owner(message.from_user.id):
        storage.put("editing_welcome", "0")
        storage.put("creating_collection", "0")
        if command.args and command.args.startswith("topic_"):
            collection = storage.collection_by_id(command.args.removeprefix("topic_"))
            if collection:
                await show_collection(message, bot, collection)
                return
        me = await bot.get_me()
        await message.answer(
            "Вы владелец этого бота.\n\n"
            f"Дайте людям ссылку https://t.me/{me.username}?start=general — в описании канала, профиля или в сторис. "
            "Вопросы будут приходить сюда без имени автора.\n\n"
            "Чтобы ответить — нажмите на вопрос → «Ответить» и напишите ответ (можно фото или голосовое).\n"
            "/ban — ответом на вопрос: больше не принимать сообщения от этого автора.\n"
            "/unban — снять все блокировки.\n"
            "/stats — сколько вопросов пришло.\n"
            "/welcome текст — своё приветствие для тех, кто откроет бота.\n\n"
            "«Новый сбор» — отдельная ссылка для вопросов к конкретному посту или теме.",
            reply_markup=owner_menu(),
        )
        return
    collection = None
    if command.args and command.args.startswith("topic_"):
        collection = storage.collection_by_id(command.args.removeprefix("topic_"))
    storage.select_collection(message.from_user.id, collection.id if collection else None)
    if collection:
        await message.answer(collection_invitation(collection), reply_markup=general_question_button())
        return
    if command.args and command.args != "general":
        await message.answer(
            "Этот сбор не найден. Откройте актуальную ссылку под постом "
            "или напишите здесь общий вопрос — он уйдёт без темы.",
        )
        return
    await message.answer(welcome_text())


@dp.callback_query(F.data == "guest:general")
async def general_question(query: CallbackQuery) -> None:
    if not query.message or query.message.chat.type != "private" or query.from_user.id == storage.owner_id():
        await query.answer()
        return
    storage.select_collection(query.from_user.id, None)
    await query.answer()
    await query.message.answer(f"Теперь можно задать общий вопрос без темы.\n\n{welcome_text()}")


@dp.message(Command("welcome"), is_owner)
async def welcome(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer(
            "Напишите текст сразу после команды, например:\n"
            "/welcome Спрашивай что угодно — я не узнаю, кто ты\n\n"
            f"Сейчас гостей встречает:\n{welcome_text()}"
        )
        return
    if len(command.args) > MAX_WELCOME:
        await message.answer(f"Слишком длинно. Нужно до {MAX_WELCOME} символов.")
        return
    storage.put("welcome", command.args)
    storage.put("editing_welcome", "0")
    storage.put("creating_collection", "0")
    await message.answer(f"Готово. Так бот встретит гостей:\n\n{command.args}", reply_markup=owner_menu())


@dp.message(Command("ban"), is_owner)
async def ban(message: Message) -> None:
    target = message.reply_to_message and storage.author_of(message.reply_to_message.message_id)
    if not target:
        await message.answer("Отправьте /ban ответом на вопрос того, кого хотите заблокировать.")
        return
    storage.ban(target)
    await message.answer("Заблокировал. Сообщения от этого автора больше не придут. /unban — снять все блокировки.")


@dp.message(Command("unban"), is_owner)
async def unban(message: Message) -> None:
    await message.answer(f"Снял блокировок: {storage.unban_all()}.")


@dp.message(Command("stats"), is_owner)
async def stats(message: Message) -> None:
    await message.answer(f"Всего вопросов: {storage.question_count()}.")


@dp.message(is_owner, F.reply_to_message)
async def answer(message: Message, bot: Bot) -> None:
    author = storage.author_of(message.reply_to_message.message_id)
    if author is None:
        if storage.get("creating_collection") == "1":
            await owner_hint(message, bot)
            return
        await message.answer("Не нашёл, чей это вопрос. Отвечайте именно на сообщение с вопросом.")
        return
    try:
        await bot.send_message(author, "Вам ответили:")
        await bot.copy_message(author, message.chat.id, message.message_id)
    except TelegramAPIError:
        await message.answer("Не получилось доставить: автор, похоже, заблокировал бота.")
        return
    # Only a delivery flag for the hosted pilot. No sender, content or message IDs.
    if os.getenv("BOTDEPO_MEASURE_REPLY") == "1":
        try:
            (storage.DATA_DIR / ".first-answer-delivered").touch(exist_ok=False)
        except FileExistsError:
            pass
        except OSError:
            logging.warning("Could not record delivery flag")
    await message.answer("Отправил ответ.")


@dp.message(is_owner)
async def owner_hint(message: Message, bot: Bot) -> None:
    if message.chat.type != "private":
        return
    if message.text == "/cancel":
        creating = storage.get("creating_collection") == "1"
        storage.put("editing_welcome", "0")
        storage.put("creating_collection", "0")
        await message.answer(
            "Создание сбора отменено." if creating else "Приветствие не изменилось.",
            reply_markup=owner_menu(),
        )
    elif storage.get("creating_collection") == "1":
        if not message.text or message.text.startswith("/"):
            await message.answer("Пришлите название сбора текстом. /cancel — отменить.")
            return
        try:
            collection = storage.create_collection(message.text)
        except ValueError as error:
            await message.answer(f"{error} Пришлите другое название. /cancel — отменить.")
            return
        storage.put("creating_collection", "0")
        await show_collection(message, bot, collection)
    elif storage.get("editing_welcome") == "1" and message.text and not message.text.startswith("/"):
        if len(message.text) > MAX_WELCOME:
            await message.answer(f"Нужно до {MAX_WELCOME} символов. Сократите текст или отправьте /cancel.")
            return
        storage.put("welcome", message.text)
        storage.put("editing_welcome", "0")
        await message.answer("Приветствие сохранено. Следующий гость увидит новый текст.", reply_markup=owner_menu())
    else:
        await message.answer("Чтобы ответить, нажмите на вопрос → «Ответить». Настройки — кнопками ниже.", reply_markup=owner_menu())


@dp.message(F.chat.type == "private")
async def question(message: Message, bot: Bot) -> None:
    owner = storage.owner_id()
    if owner is None:
        await message.answer("Бот ещё не настроен. Владелец должен первым написать /start.")
        return
    if message.text and message.text.startswith("/"):
        await message.answer("Просто напишите вопрос — команды не нужны.")
        return
    if storage.is_banned(message.from_user.id):
        return  # молча: заблокированному незачем знать, что его заблокировали
    # Freeze the context before any network await: another /start can switch it.
    collection = storage.selected_collection(message.from_user.id)
    now = time.monotonic()
    if now - _last_question.get(message.from_user.id, -100) < 10:
        await message.answer("Вопрос уже отправлен. Следующий можно отправить через 10 секунд.")
        return
    if len(_last_question) > 5000:
        for user, sent in list(_last_question.items()):
            if now - sent > 60:
                del _last_question[user]
    _last_question[message.from_user.id] = now  # reserve before awaiting Telegram: updates run concurrently
    try:
        notice = await bot.send_message(
            owner,
            f"Новый анонимный вопрос\nСбор: {collection.title}" if collection else "Новый анонимный вопрос:",
        )
        # copy_message, а не forward: пересылка показала бы имя автора
        if collection:
            copied = await bot.copy_message(
                owner, message.chat.id, message.message_id,
                reply_parameters=ReplyParameters(message_id=notice.message_id),
            )
        else:
            copied = await bot.copy_message(owner, message.chat.id, message.message_id)
    except TelegramAPIError:
        if _last_question.get(message.from_user.id) == now:
            _last_question.pop(message.from_user.id, None)
        await message.answer("Не удалось доставить вопрос. Возможно, владелец ещё не нажал /start. Попробуйте позже.")
        return
    storage.remember_question(copied.message_id, message.from_user.id, collection.id if collection else None)
    if collection:
        await message.answer(
            f"Вопрос по теме «{collection.title}» отправлен без имени отправителя. Ответ придёт сюда.",
            reply_markup=general_question_button(),
        )
    else:
        await message.answer("Отправил анонимно. Ответ придёт сюда.")


async def main() -> None:
    storage.owner_id()  # fail closed on malformed or conflicting OWNER_ID before polling
    session = AiohttpSession(api=TEST) if os.getenv("BOTDEPO_TELEGRAM_TEST") == "1" else None
    bot = Bot(token=os.environ["BOT_TOKEN"], session=session)
    me = await bot.get_me()
    logging.info("бот запущен: @%s", me.username)
    if storage.owner_id() is None:
        logging.info("владельца пока нет: напишите боту /start первым — станете владельцем")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
