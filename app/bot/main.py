from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.bot.handlers import agent, client
from app.config import settings
from app.db import engine

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    if not settings.BOT_TOKEN:
        logger.error(
            "BOT_TOKEN is not configured. Copy .env.example to .env and set BOT_TOKEN."
        )
        raise SystemExit(1)

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(agent.router)
    dispatcher.include_router(client.router)

    commands = [
        BotCommand(command="start", description="Начать / продолжить"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="agent_start", description="Регистрация риелтора"),
        BotCommand(command="add_listing", description="Добавить листинг"),
        BotCommand(command="my_listings", description="Мои листинги"),
        BotCommand(command="my_leads", description="Мои лиды"),
        BotCommand(command="import_excel", description="📥 Загрузить базу из Excel"),
    ]
    await bot.set_my_commands(commands=commands)

    logger.info("Starting bot polling")
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        await engine.dispose()
