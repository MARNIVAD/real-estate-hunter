from __future__ import annotations

import html
import logging
from decimal import Decimal
from typing import Any

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bot.keyboards import get_main_menu_keyboard
from app.bot.states import ClientFlow
from app.db import async_session_factory
from app.models import Agent, Client, ClientAgentLink, Dialog, Lead, Listing
from app.services.llm import extract_slots, _question_for
from app.services.notifier import format_price, send_lead_to_agent
from app.services.search import find_listings, find_listings_fallback

logger = logging.getLogger(__name__)
router = Router(name="client")

_NO_AGENT_TEXT = (
    "Привет! Я бот для подбора квартир в Дананге.\n"
    "Пожалуйста, перейдите по ссылке от вашего риелтора."
)
_GREETING = (
    "Здравствуйте! Опишите, какую квартиру ищете: количество комнат, "
    "бюджет, район и другие пожелания."
)


async def _get_or_create_client(session: AsyncSession, message: Message) -> Client:
    user = message.from_user
    stmt = select(Client).where(Client.telegram_id == user.id)
    client = (await session.execute(stmt)).scalar_one_or_none()
    language = user.language_code if user.language_code in {"ru", "en", "vi"} else None
    if client is None:
        client = Client(
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            language=language,
        )
        session.add(client)
        await session.flush()
    else:
        client.username = user.username
        client.first_name = user.first_name
        if language:
            client.language = language
    return client


async def _link_client_to_agent(
    session: AsyncSession, client_id: int, agent_id: int, source: str
) -> None:
    stmt = select(ClientAgentLink).where(
        ClientAgentLink.client_id == client_id,
        ClientAgentLink.agent_id == agent_id,
    )
    link = (await session.execute(stmt)).scalar_one_or_none()
    if link is None:
        session.add(ClientAgentLink(client_id=client_id, agent_id=agent_id, source=source))


async def _find_existing_agent_id(session: AsyncSession, client_id: int) -> int | None:
    stmt = (
        select(ClientAgentLink.agent_id)
        .join(Agent, Agent.id == ClientAgentLink.agent_id)
        .where(ClientAgentLink.client_id == client_id, Agent.is_active.is_(True))
        .order_by(ClientAgentLink.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _get_or_create_dialog(
    session: AsyncSession, client_id: int, agent_id: int, language: str | None
) -> Dialog:
    stmt = (
        select(Dialog)
        .where(Dialog.client_id == client_id, Dialog.agent_id == agent_id)
        .order_by(Dialog.updated_at.desc())
        .limit(1)
    )
    dialog = (await session.execute(stmt)).scalar_one_or_none()
    if dialog is None:
        dialog = Dialog(
            client_id=client_id,
            agent_id=agent_id,
            state=ClientFlow.waiting_query.state,
            state_json={"slots": {}, "language": language, "history": []},
            is_active=True,
        )
        session.add(dialog)
        await session.flush()
    return dialog


def _format_listing(listing: Listing) -> str:
    rooms = listing.rooms
    if rooms == 0:
        rooms_label = "Студия"
    elif rooms is not None:
        rooms_label = f"{rooms}-комн. квартира"
    else:
        rooms_label = "Квартира"

    details = [f"<b>{html.escape(listing.title)}</b>", rooms_label]
    if listing.area_m2 is not None:
        details.append(f"Площадь: {float(listing.area_m2):.0f} м²")
    if listing.district:
        details.append(f"Район: {html.escape(listing.district)}")
    if listing.pets_allowed is not None:
        details.append("Питомцы: да" if listing.pets_allowed else "Питомцы: нет")
    details.append(f"Цена: {format_price(listing.price, listing.currency)}")
    if listing.description:
        details.append("")
        details.append(html.escape(listing.description[:500]))
    return "\n".join(details)


async def _send_listings(message: Message, listings: list[Listing]) -> None:
    for listing in listings:
        caption = _format_listing(listing)[:1024]
        try:
            if listing.photos:
                await message.answer_photo(
                    photo=listing.photos[0].telegram_file_id, caption=caption
                )
                for photo in listing.photos[1:4]:
                    await message.answer_photo(photo=photo.telegram_file_id)
            else:
                await message.answer(caption, disable_web_page_preview=True)
        except TelegramAPIError as exc:
            logger.error("Failed to send listing_id=%s: %s", listing.id, exc)


def _merge_slots(previous: dict[str, Any], extracted: dict[str, Any]) -> dict[str, Any]:
    merged = dict(previous)
    for key, value in extracted.items():
        if value is not None:
            merged[key] = value
    return merged


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    payload = (message.text or "").split(maxsplit=1)
    deep_link = payload[1].strip() if len(payload) > 1 else None

    async with async_session_factory() as session:
        client = await _get_or_create_client(session, message)

        agent: Agent | None = None
        source = "deep_link"
        if deep_link and deep_link.startswith("agent_"):
            try:
                agent_id = int(deep_link.removeprefix("agent_"))
            except ValueError:
                agent_id = 0
            if agent_id:
                agent = (
                    await session.execute(
                        select(Agent).where(Agent.id == agent_id, Agent.is_active.is_(True))
                    )
                ).scalar_one_or_none()
            if agent is None:
                await session.commit()
                await message.answer("Ссылка недействительна или риелтор неактивен.")
                await state.clear()
                return
            await _link_client_to_agent(session, client.id, agent.id, source)
        else:
            agent_id = await _find_existing_agent_id(session, client.id)
            if agent_id:
                agent = (
                    await session.execute(
                        select(Agent).where(Agent.id == agent_id, Agent.is_active.is_(True))
                    )
                ).scalar_one_or_none()
                source = "existing_link"

        if agent is None:
            await session.commit()
            await message.answer(_NO_AGENT_TEXT)
            await state.clear()
            return

        language = client.language if client.language in {"ru", "en", "vi"} else "ru"
        dialog = await _get_or_create_dialog(session, client.id, agent.id, language)
        dialog.state = ClientFlow.waiting_query.state
        dialog.state_json = {"slots": {}, "language": language, "history": []}
        await state.set_state(ClientFlow.waiting_query)
        await state.update_data(agent_id=agent.id, language=language, slots={}, history=[])
        await session.commit()

    await message.answer(
        _GREETING,
        reply_markup=get_main_menu_keyboard() if agent is not None else None,
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Диалог сброшен. Отправьте новый запрос или /start.")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    async with async_session_factory() as session:
        agent_stmt = select(Agent).where(Agent.telegram_id == message.from_user.id)
        agent = (await session.execute(agent_stmt)).scalar_one_or_none()
    if agent is not None:
        await message.answer(
            "Вы зарегистрированы как риелтор. Ваши команды:\n"
            "/add_listing — добавить листинг\n"
            "/my_listings — мои листинги\n"
            "/my_leads — мои лиды\n"
            "/import_excel — загрузить базу из Excel",
            reply_markup=get_main_menu_keyboard(),
        )
        return
    await message.answer(
        "Отправьте описание квартиры одним или несколькими сообщениями. "
        "Я уточню недостающие параметры и подберу варианты."
    )


@router.message(lambda message: bool(message.text) and not message.text.startswith("/"))
async def handle_client_message(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        return

    async with async_session_factory() as session:
        telegram_id = message.from_user.id
        agent = (
            await session.execute(select(Agent).where(Agent.telegram_id == telegram_id))
        ).scalar_one_or_none()
        if agent is not None:
            await message.answer(
                "Вы зарегистрированы как риелтор. Используйте /add_listing, "
                "/my_listings или /my_leads."
            )
            return

        client = await _get_or_create_client(session, message)
        data = await state.get_data()
        agent_id = data.get("agent_id")
        if not agent_id:
            agent_id = await _find_existing_agent_id(session, client.id)
        if not agent_id:
            await message.answer(_NO_AGENT_TEXT)
            await state.clear()
            return

        agent = (
            await session.execute(
                select(Agent).where(Agent.id == agent_id, Agent.is_active.is_(True))
            )
        ).scalar_one_or_none()
        if agent is None:
            await message.answer("Риелтор неактивен. Попросите у него новую ссылку.")
            await state.clear()
            return

        previous_language = data.get("language") or client.language
        dialog = await _get_or_create_dialog(session, client.id, agent.id, previous_language)
        current_state = await state.get_state()

        is_follow_up = current_state == ClientFlow.waiting_slot.state
        previous_slots: dict[str, Any] = data.get("slots", {}) if is_follow_up else {}
        history: list[str] = list(data.get("history", [])) if is_follow_up else []
        history.append(text)
        history = history[-10:]

        extraction = await extract_slots(text, language_hint=previous_language)
        merged_slots = _merge_slots(previous_slots, extraction["slots"])

        result_language = extraction.get("language") or previous_language or "ru"
        if len(text) <= 3 and previous_language in {"ru", "en", "vi"}:
            result_language = previous_language
        if result_language not in {"ru", "en", "vi"}:
            result_language = "ru"

        missing = [
            slot for slot in ("rooms", "budget_max") if merged_slots.get(slot) is None
        ]
        is_complete = not missing

        dialog.state_json = {
            "slots": merged_slots,
            "language": result_language,
            "history": history,
            "missing": missing,
            "last_message": text,
        }

        if not is_complete:
            dialog.state = ClientFlow.waiting_slot.state
            await state.set_state(ClientFlow.waiting_slot)
            await state.update_data(
                agent_id=agent.id,
                language=result_language,
                slots=merged_slots,
                history=history,
            )
            await session.commit()

            question = extraction.get("next_question") or _question_for(missing[0], result_language)
            await message.answer(html.escape(question))
            return

        dialog.state = ClientFlow.processing.state
        await state.set_state(ClientFlow.processing)
        await session.commit()

        listings = await find_listings(session, agent.id, merged_slots, limit=5)
        relaxed_what = None
        if not listings:
            fallback = await find_listings_fallback(session, agent.id, merged_slots, limit=3)
            listings = fallback["listings"]
            relaxed_what = fallback["relaxed_what"]

        lead = Lead(
            client_id=client.id,
            agent_id=agent.id,
            listing_id=listings[0].id if listings else None,
            original_message="\n".join(history),
            original_language=result_language,
            slots=merged_slots,
            status="new",
        )
        session.add(lead)
        await session.flush()
        lead_id = lead.id

        dialog.state = ClientFlow.results.state
        dialog.state_json = {
            "slots": merged_slots,
            "language": result_language,
            "history": history,
            "missing": [],
            "last_message": text,
            "lead_id": lead_id,
        }
        await session.commit()

        if listings:
            if relaxed_what:
                await message.answer(
                    "Точных совпадений нет, но вот варианты с ослабленными условиями "
                    f"({html.escape(relaxed_what)}):"
                )
            else:
                await message.answer("Вот подходящие варианты:")
            await _send_listings(message, listings)
        else:
            await message.answer(
                "Пока нет подходящих вариантов. Передал запрос риелтору, он свяжется с вами."
            )

        await send_lead_to_agent(message.bot, session, lead_id)

        await state.set_state(ClientFlow.results)
        await state.update_data(
            agent_id=agent.id,
            language=result_language,
            slots=merged_slots,
            history=history,
        )
