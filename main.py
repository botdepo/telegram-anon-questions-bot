"""Бот анонимных вопросов.

Кто угодно пишет боту — владелец получает вопрос без имени автора и
отвечает обычным «Ответить» в Telegram; бот пересылает ответ автору.
Автор так и остаётся анонимным: бот никому не показывает, кто спросил.

Настраивать ничего не нужно: кто первым напишет боту /start — тот
владелец. Ссылку на бота можно поставить в описание канала или профиля.

Токен бот получает из переменной окружения BOT_TOKEN — на botdepo её
подставляет панель, локально задайте сами (см. README).
"""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

dp = Dispatcher()


async def is_owner(event: Message | CallbackQuery) -> bool:
    # async, а не F.func: синхронные фильтры aiogram гоняет в другом потоке, а SQLite так не умеет
    return event.from_user.id == storage.owner_id()


@dp.message(CommandStart())
async def start(message: Message, bot: Bot) -> None:
    if storage.claim_owner(message.from_user.id):
        me = await bot.get_me()
        await message.answer(
            "Вы владелец этого бота.\n\n"
            f"Дайте людям ссылку t.me/{me.username} — в описании канала, профиля или в сторис. "
            "Вопросы будут приходить сюда без имени автора.\n\n"
            "Чтобы ответить — нажмите на вопрос → «Ответить» и напишите ответ (можно фото или голосовое).\n"
            "/ban — ответом на вопрос: больше не принимать сообщения от этого автора.\n"
            "/unban — снять все блокировки.\n"
            "/stats — сколько вопросов пришло."
        )
        return
    await message.answer(
        "Привет! Напишите свой вопрос — он придёт анонимно, никто не узнает, что это вы. "
        "Когда ответят, ответ придёт сюда."
    )


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
    await message.answer("Чтобы ответить, нажмите на вопрос → «Ответить». Команды — в /start.")


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
    await bot.send_message(owner, "Новый анонимный вопрос:")
    # copy_message, а не forward: пересылка показала бы имя автора
    copied = await bot.copy_message(owner, message.chat.id, message.message_id)
    storage.remember_question(copied.message_id, message.from_user.id)
    await message.answer("Отправил анонимно. Ответ придёт сюда.")


async def main() -> None:
    bot = Bot(token=os.environ["BOT_TOKEN"])
    me = await bot.get_me()
    logging.info("бот запущен: @%s", me.username)
    if storage.owner_id() is None:
        logging.info("владельца пока нет: напишите боту /start первым — станете владельцем")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
