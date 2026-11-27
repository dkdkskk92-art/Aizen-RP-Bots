import asyncio
import logging
from html import escape

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message, MessageEntity
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import settings
from database import Base, GameCommandAccess, Session, User, engine


GAME_COMMANDS = {
    101: ("Покормить", "покормить", 0, "🍓", "покормил"),
    102: ("Профиль", "профиль", 0, "👤", "посмотрел профиль"),
}


def utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def profile_url(user: User) -> str | None:
    return f"https://t.me/{user.username}" if user.username else None


def display_name(user: User) -> str:
    return " ".join(part for part in (user.first_name, user.last_name) if part).strip() or "Без имени"


async def get_user(session, telegram_id: int):
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


class RegistrationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        actor = getattr(event, "from_user", None)
        if actor and not actor.is_bot:
            async with Session() as session:
                user = await get_user(session, actor.id)
                if user is None:
                    user = User(
                        telegram_id=actor.id,
                        username=actor.username,
                        first_name=actor.first_name or "",
                        last_name=actor.last_name,
                        rank=0,
                    )
                    session.add(user)
                else:
                    user.username = actor.username
                    user.first_name = actor.first_name or ""
                    user.last_name = actor.last_name
                await session.commit()
        return await handler(event, data)


class CommandAccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not isinstance(event, Message) or not event.text or not event.from_user or event.from_user.is_bot:
            return await handler(event, data)

        first_line = event.text.splitlines()[0].strip().lower()
        command_id = None
        for candidate_id, (_, trigger, _, _, _) in GAME_COMMANDS.items():
            if first_line == trigger or first_line.startswith(trigger + " "):
                command_id = candidate_id
                break
        if command_id is None:
            return await handler(event, data)

        async with Session() as session:
            user = await get_user(session, event.from_user.id)
            rank = user.rank if user else 0
            setting = await session.get(GameCommandAccess, command_id)
            min_rank = setting.min_rank if setting else GAME_COMMANDS[command_id][2]

        if min_rank == 6 or rank < min_rank:
            await event.reply("⛔ У тебя нет доступа к этой игровой команде.")
            return
        return await handler(event, data)


async def resolve_target(message: Message, session, raw: str):
    replied = message.reply_to_message.from_user if message.reply_to_message else None
    if replied and not replied.is_bot:
        user = await get_user(session, replied.id)
        if user is None:
            user = User(
                telegram_id=replied.id,
                username=replied.username,
                first_name=replied.first_name or "",
                last_name=replied.last_name,
                rank=0,
            )
            session.add(user)
            await session.flush()
        return user

    token = raw.strip().split()[0] if raw.strip() else ""
    if token.startswith("@"):
        return await session.scalar(
            select(User).where(func.lower(User.username) == token[1:].lower())
        )
    if token.isdigit():
        # В командах принимается только кастомный ID (users.id).
        # Telegram ID нужен только для сопоставления входящего Telegram update.
        return await session.get(User, int(token))
    return None


async def send_rp_action(message: Message, emoji: str, verb: str):
    if not message.from_user or message.from_user.is_bot:
        return

    lines = (message.text or "").split("\n", 1)
    header = lines[0].strip()
    parts = header.split(maxsplit=1)
    target_raw = parts[1] if len(parts) > 1 else ""
    extra_raw = lines[1] if len(lines) > 1 else ""
    extra = extra_raw.strip()

    if extra and "\n" in extra:
        await message.reply("Дополнительный текст должен быть только на одной строке.")
        return
    if len(extra) > 20:
        await message.reply("Дополнительный текст не должен превышать 20 символов.")
        return

    async with Session() as session:
        actor = await get_user(session, message.from_user.id)
        target = await resolve_target(message, session, target_raw)
        if not actor:
            await message.reply("Не удалось определить твой профиль.")
            return
        if not target:
            await message.reply(
                "Пользователь не найден. Он должен хотя бы раз написать боту "
                "или появиться в группе, где бот видит сообщения."
            )
            return

        actor_name = display_name(actor)
        target_name = display_name(target)
        actor_url = profile_url(actor)
        target_url = profile_url(target)
        await session.commit()

    main_text = f"{emoji} | {actor_name} {verb} {target_name}"
    output = main_text + (f"\nТекст: {extra}" if extra else "")
    entities = []
    if actor_url:
        entities.append(MessageEntity(
            type="text_link",
            offset=utf16_length(f"{emoji} | "),
            length=utf16_length(actor_name),
            url=actor_url,
        ))
    if target_url:
        entities.append(MessageEntity(
            type="text_link",
            offset=utf16_length(f"{emoji} | {actor_name} {verb} "),
            length=utf16_length(target_name),
            url=target_url,
        ))

    if extra:
        leading = len(extra_raw) - len(extra_raw.lstrip())
        source_extra_offset = utf16_length(header + "\n" + extra_raw[:leading])
        output_extra_offset = utf16_length(main_text + "\nТекст: ")
        for entity in message.entities or []:
            if entity.type == "custom_emoji" and entity.custom_emoji_id:
                relative = entity.offset - source_extra_offset
                if 0 <= relative and relative + entity.length <= utf16_length(extra):
                    entities.append(
                        MessageEntity(
                            type="custom_emoji",
                            offset=output_extra_offset + relative,
                            length=entity.length,
                            custom_emoji_id=entity.custom_emoji_id,
                        )
                    )

    try:
        await message.answer(output, entities=entities, disable_web_page_preview=True)
    except TelegramBadRequest:
        # При отсутствии у бота права отправлять custom emoji остаётся обычный emoji fallback.
        fallback_entities = [entity for entity in entities if entity.type != "custom_emoji"]
        await message.answer(output, entities=fallback_entities, disable_web_page_preview=True)


async def main():
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_file_id VARCHAR(255)"))
        await connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_type VARCHAR(10)"))

    async with Session() as session:
        defaults = [
            {"command_id": command_id, "min_rank": meta[2]}
            for command_id, meta in GAME_COMMANDS.items()
        ]
        await session.execute(
            pg_insert(GameCommandAccess).values(defaults).on_conflict_do_nothing(index_elements=["command_id"])
        )
        await session.commit()

    bot = Bot(settings.bot_token)
    dispatcher = Dispatcher()
    dispatcher.message.outer_middleware(RegistrationMiddleware())
    dispatcher.message.outer_middleware(CommandAccessMiddleware())

    @dispatcher.message(F.text.regexp(r"(?i)^дк(?:\s|$)"))
    async def command_access_handler(message: Message):
        if not message.from_user or message.from_user.is_bot:
            return
        parts = (message.text or "").split()
        async with Session() as session:
            actor = await get_user(session, message.from_user.id)
            if not actor or actor.rank != 5:
                await message.reply("⛔ Настраивать доступы игровых команд может только владелец (5 ранг).")
                return

            if len(parts) == 1:
                rows = (await session.scalars(select(GameCommandAccess))).all()
                access_map = {row.command_id: row.min_rank for row in rows}
                lines = [
                    "🎮 <b>Настройки доступов игровых команд</b>",
                    "0 — всем; 1–5 — с указанного ранга и выше; 6 — запрещено всем.",
                ]
                for command_id, meta in GAME_COMMANDS.items():
                    value = access_map.get(command_id, meta[2])
                    lines.append(f"<code>{command_id}</code> — {escape(meta[0])} (<code>{value}</code>)")
                await message.reply("\n".join(lines), parse_mode="HTML")
                return

            if not parts[1].isdigit() or int(parts[1]) not in GAME_COMMANDS:
                await message.reply("Укажи существующий номер игровой команды. Посмотреть список: «дк».")
                return

            command_id = int(parts[1])
            setting = await session.get(GameCommandAccess, command_id)
            current = setting.min_rank if setting else GAME_COMMANDS[command_id][2]
            if len(parts) == 2:
                await message.reply(
                    f"Игровая команда №{command_id} — {GAME_COMMANDS[command_id][0]}. Текущий доступ: {current}."
                )
                return

            if len(parts) != 3 or not parts[2].isdigit() or not 0 <= int(parts[2]) <= 6:
                await message.reply("Формат: дк {номер команды} {доступ от 0 до 6}.")
                return

            new_access = int(parts[2])
            if setting is None:
                session.add(GameCommandAccess(command_id=command_id, min_rank=new_access))
            else:
                setting.min_rank = new_access
            await session.commit()

        await message.reply(
            f"✅ Доступ игровой команды №{command_id} «{GAME_COMMANDS[command_id][0]}» установлен: {new_access}."
        )

    @dispatcher.message(F.text.regexp(r"(?i)^мдк\s*$"))
    async def my_access_handler(message: Message):
        async with Session() as session:
            actor = await get_user(session, message.from_user.id)
            rank = actor.rank if actor else 0
            rows = (await session.scalars(select(GameCommandAccess))).all()
            access_map = {row.command_id: row.min_rank for row in rows}

        available = [
            (command_id, meta[0])
            for command_id, meta in GAME_COMMANDS.items()
            if access_map.get(command_id, meta[2]) != 6
            and rank >= access_map.get(command_id, meta[2])
        ]
        if not available and rank != 5:
            await message.reply("У тебя пока нет доступных игровых команд.")
            return

        lines = [f"🎮 <b>Твои доступные игровые команды</b> (ранг {rank})"]
        lines.extend(
            f"<code>{command_id}</code> — {title}"
            for command_id, title in available
        )
        if rank == 5:
            lines.append("<code>дк</code> — настройка доступов игровых команд")
        await message.reply("\n".join(lines), parse_mode="HTML")

    @dispatcher.message(F.text.regexp(r"(?is)^покормить(?:\s|$)"))
    async def feed_handler(message: Message):
        await send_rp_action(message, "🍓", "покормил")

    @dispatcher.message(F.text.regexp(r"(?i)^профиль(?:\s+\d+)?\s*$"))
    async def profile_handler(message: Message):
        if not message.from_user or message.from_user.is_bot:
            return
        parts = (message.text or "").split()
        async with Session() as session:
            if len(parts) > 1:
                actor = await session.get(User, int(parts[1]))
                if actor is None:
                    await message.reply("Пользователь с таким кастомным ID не найден.")
                    return
            else:
                actor = await get_user(session, message.from_user.id)

            if actor is None:
                await message.reply("Профиль не найден. Напиши боту ещё раз, чтобы пройти регистрацию.")
                return

            name = escape(display_name(actor))
            username = f"@{escape(actor.username)}" if actor.username else "не указан"
            await message.reply(
                "👤 <b>Профиль игрока</b>\n"
                f"🆔 Кастомный ID: <code>{actor.id}</code>\n"
                f"Имя: {name}\n"
                f"Username: {username}\n"
                f"Ранг: {actor.rank}",
                parse_mode="HTML",
            )

    @dispatcher.message(F.text.regexp(r"(?i)^/start(?:\s|$)"))
    async def start_handler(message: Message):
        await message.answer(
            "Привет! Я игровой бот Айзена.\n"
            "Команда «профиль» — посмотреть свой профиль.\n"
            "Команда «покормить» — покормить участника.\n"
            "Команда «мдк» — посмотреть доступные игровые команды."
        )

    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
