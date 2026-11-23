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
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TEST
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup

import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

dp = Dispatcher()

DEFAULT_WELCOME = (
    "Привет! Напишите свой вопрос — он придёт владельцу без имени отправителя. "
    "Когда ответят, ответ придёт сюда."
)
MAX_WELCOME = 2000
_last_question: dict[int, float] = {}


def owner_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Моя ссылка", callback_data="owner:link"),
         InlineKeyboardButton(text="Приветствие", callback_data="owner:welcome")],
        [InlineKeyboardButton(text="Статистика", callback_data="owner:stats"),
         InlineKeyboardButton(text="Как отвечать", callback_data="owner:help")],
    ])


@dp.callback_query(F.data.startswith("owner:"))
async def owner_button(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    if not query.message or query.message.chat.type != "private" or query.from_user.id != storage.owner_id():
        return
    action = query.data.split(":", 1)[1]
    if action == "link":
        me = await bot.get_me()
        await query.message.answer(f"Ваша ссылка:\nhttps://t.me/{me.username}\n\nПоставьте её в профиль или описание канала.")
    elif action == "stats":
        await query.message.answer(f"Получено вопросов: {storage.question_count()}.", reply_markup=owner_menu())
    elif action == "welcome":
        storage.put("editing_welcome", "1")
        await query.message.answer(
            f"Пришлите новое приветствие одним текстовым сообщением, до {MAX_WELCOME} символов.\n\n"
            f"Сейчас:\n{welcome_text()}\n\n/cancel — оставить как есть."
        )
    elif action == "help":
        await query.message.answer(
            "Нажмите на сообщение с вопросом → «Ответить» и напишите ответ. Можно фото или голосовое.\n\n"
            "/ban ответом на вопрос — заблокировать автора.\n/unban — снять все блокировки.\n\n"
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
async def start(message: Message, bot: Bot) -> None:
    if message.chat.type != "private":
        return
    if storage.claim_owner(message.from_user.id):
        me = await bot.get_me()
        await message.answer(
            "Вы владелец этого бота.\n\n"
            f"Дайте людям ссылку t.me/{me.username} — в описании канала, профиля или в сторис. "
            "Вопросы будут приходить сюда без имени автора.\n\n"
            "Чтобы ответить — нажмите на вопрос → «Ответить» и напишите ответ (можно фото или голосовое).\n"
            "/ban — ответом на вопрос: больше не принимать сообщения от этого автора.\n"
            "/unban — снять все блокировки.\n"
            "/stats — сколько вопросов пришло.\n"
            "/welcome текст — своё приветствие для тех, кто откроет бота.",
            reply_markup=owner_menu(),
        )
        return
    await message.answer(welcome_text())


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
        await message.answer("Не нашёл, чей это вопрос. Отвечайте именно на сообщение с вопросом.")
        return
    try:
        await bot.send_message(author, "Вам ответили:")
        await bot.copy_message(author, message.chat.id, message.message_id)
    except TelegramAPIError:
        await message.answer("Не получилось доставить: автор, похоже, заблокировал бота.")
        return
    await message.answer("Отправил ответ.")


@dp.message(is_owner)
async def owner_hint(message: Message) -> None:
    if message.chat.type != "private":
        return
    if message.text == "/cancel":
        storage.put("editing_welcome", "0")
        await message.answer("Приветствие не изменилось.", reply_markup=owner_menu())
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
        await bot.send_message(owner, "Новый анонимный вопрос:")
        # copy_message, а не forward: пересылка показала бы имя автора
        copied = await bot.copy_message(owner, message.chat.id, message.message_id)
    except TelegramAPIError:
        if _last_question.get(message.from_user.id) == now:
            _last_question.pop(message.from_user.id, None)
        await message.answer("Не удалось доставить вопрос. Возможно, владелец ещё не нажал /start. Попробуйте позже.")
        return
    storage.remember_question(copied.message_id, message.from_user.id)
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
