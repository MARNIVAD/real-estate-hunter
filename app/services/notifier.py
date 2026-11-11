from __future__ import annotations

import html
import logging
from decimal import Decimal
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bot.keyboards import contact_client_keyboard
from app.models import Lead
from app.services.llm import translate_to_ru
from app.services.search import find_listings, find_listings_fallback

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    "new": "новый",
    "contacted": "связались",
    "closed": "закрыт",
}


def format_price(price: Decimal | float | None, currency: str) -> str:
    if price is None:
        return "цена не указана"
    value = float(price)
    if currency == "VND":
        return f"{value:,.0f} VND".replace(",", " ")
    return f"${value:,.0f}".replace(",", " ")


def format_slots(slots: dict[str, Any]) -> str:
    lines: list[str] = []
    rooms = slots.get("rooms")
    if rooms is not None:
        lines.append("Комнат: студия" if int(rooms) == 0 else f"Комнат: {rooms}")
    if slots.get("budget_max") is not None:
        lines.append(
            f"Бюджет до: {format_price(slots['budget_max'], slots.get('budget_currency') or 'USD')}"
        )
    if slots.get("area_m2_min") is not None:
        lines.append(f"Площадь от: {slots['area_m2_min']} м²")
    if slots.get("district"):
        lines.append(f"Район: {slots['district']}")
    if slots.get("pets_allowed") is not None:
        lines.append("Питомцы: да" if slots["pets_allowed"] else "Питомцы: нет")
    if slots.get("amenities"):
        lines.append("Удобства: " + ", ".join(str(item) for item in slots["amenities"]))
    return "\n".join(lines) if lines else "Слоты не распознаны"


def _lead_text(lead: Lead, listings: list, translated: str) -> str:
    client = lead.client
    client_label = client.first_name or "Клиент"
    if client.username:
        client_label += f" (@{client.username})"
    client_label += f", telegram_id={client.telegram_id}"

    listings_text = (
        "\n".join(
            f"#{listing.id} {listing.title} — {format_price(listing.price, listing.currency)}"
            for listing in listings
        )
        if listings
        else "Подходящих листингов не найдено"
    )

    return (
        "<b>Новый лид</b>\n"
        f"Клиент: {html.escape(client_label)}\n"
        f"Статус: {_STATUS_LABELS.get(lead.status, lead.status)}\n\n"
        "<b>Исходное сообщение</b>\n"
        f"{html.escape(lead.original_message[:1500])}\n\n"
        "<b>Перевод на русский</b>\n"
        f"{html.escape(translated[:1500])}\n\n"
        "<b>Извлечённые слоты</b>\n"
        f"{html.escape(format_slots(lead.slots))}\n\n"
        "<b>Найденные листинги</b>\n"
        f"{html.escape(listings_text)}"
    )


async def send_lead_to_agent(bot: Bot, session: AsyncSession, lead_id: int) -> None:
    """Notify an agent about a new lead with translation and matched listings."""
    stmt = (
        select(Lead)
        .options(
            selectinload(Lead.client),
            selectinload(Lead.agent),
            selectinload(Lead.listing),
        )
        .where(Lead.id == lead_id)
    )
    lead = (await session.execute(stmt)).scalar_one_or_none()
    if lead is None:
        logger.warning("Cannot notify: lead_id=%s not found", lead_id)
        return

    agent = lead.agent
    if agent is None or not agent.is_active:
        logger.warning("Cannot notify: agent for lead_id=%s is missing/inactive", lead_id)
        return

    translated = lead.translated_message
    if not translated:
        translated = await translate_to_ru(lead.original_message, lead.original_language)
        lead.translated_message = translated
        await session.commit()

    listings = await find_listings(session, lead.agent_id, lead.slots, limit=5)
    if not listings:
        fallback = await find_listings_fallback(session, lead.agent_id, lead.slots, limit=3)
        listings = fallback["listings"]

    text = _lead_text(lead, listings, translated)
    keyboard = contact_client_keyboard(lead.client.username, lead.client.id)

    try:
        await bot.send_message(
            chat_id=agent.telegram_id,
            text=text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except TelegramAPIError as exc:
        logger.error("Failed to send lead notification to agent_id=%s: %s", agent.id, exc)
