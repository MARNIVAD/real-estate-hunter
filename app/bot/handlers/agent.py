from __future__ import annotations

import html
import io
import logging
import re
from decimal import Decimal
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InputMediaPhoto, Message
from openpyxl import load_workbook
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from httpx import AsyncClient

from app.bot.keyboards import (
    agent_main_menu,
    confirm_listing_keyboard,
    listing_actions_keyboard,
    listing_edit_keyboard,
    pets_keyboard,
)
from app.bot.states import AgentFlow
from app.db import async_session_factory
from app.models import Agent, Client, ImportError, ImportJob, Lead, Listing, ListingPhoto
from app.services.notifier import format_price, format_slots

logger = logging.getLogger(__name__)
router = Router(name="agent")

_LEAD_STATUS_LABELS = {
    "new": "новый",
    "contacted": "связались",
    "closed": "закрыт",
}

_AGENT_MENU_ACTIONS: dict[str, str] = {
    "📥 Импорт Excel": "import",
    "➕ Добавить квартиру": "add_listing",
    "📋 Мои квартиры": "my_listings",
    "👥 Мои лиды": "my_leads",
    "❓ Помощь": "help",
}


async def _get_agent(session: AsyncSession, telegram_id: int) -> Agent | None:
    stmt = select(Agent).where(Agent.telegram_id == telegram_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _require_agent(message: Message) -> Agent | None:
    async with async_session_factory() as session:
        agent = await _get_agent(session, message.from_user.id)
    if agent is None:
        await message.answer(
            "Сначала зарегистрируйтесь как риелтор: /agent_start"
        )
    return agent


async def _get_agent_listings(agent_id: int) -> list[Listing]:
    async with async_session_factory() as session:
        stmt = (
            select(Listing)
            .where(Listing.agent_id == agent_id)
            .order_by(Listing.created_at.desc())
            .limit(50)
        )
        return list((await session.execute(stmt)).scalars().all())


def _listing_display_number(listings: list[Listing], listing_id: int) -> int:
    for number, listing in enumerate(listings, start=1):
        if listing.id == listing_id:
            return number
    return listing_id


def _parse_int(text: str) -> int | None:
    match = re.search(r"\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _parse_price(text: str) -> tuple[Decimal, str] | None:
    match = re.search(r"\d[\d\s.,]*", text)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(0))
    if not digits:
        return None
    lowered = text.lower()
    currency = "VND" if ("vnd" in lowered or "₫" in text or "донг" in lowered) else "USD"
    return Decimal(digits), currency


def _rooms_label(rooms: int | None) -> str:
    if rooms == 0:
        return "Студия"
    if rooms is None:
        return "Не указано"
    return f"{rooms}"


def _pets_label(value: bool | None) -> str:
    if value is None:
        return "Неважно"
    return "Можно с питомцами" if value else "Без питомцев"


def _listing_title(rooms: int, district: str) -> str:
    if rooms == 0:
        return f"Студия, {district}"
    return f"{rooms}-комн. квартира, {district}"


async def _ask_photos(message: Message) -> None:
    await message.answer(
        "Отправьте фотографии листинга (можно несколько).\n"
        "Когда закончите — /done. Чтобы пропустить — /skip."
    )


@router.message(Command("agent_start"))
async def cmd_agent_start(message: Message, state: FSMContext) -> None:
    async with async_session_factory() as session:
        agent = await _get_agent(session, message.from_user.id)
    if agent is not None:
        await state.clear()
        await message.answer(
            "Вы уже зарегистрированы как риелтор.",
            reply_markup=agent_main_menu(),
        )
        return
    await state.set_state(AgentFlow.registration_name)
    await message.answer("Введите ваше имя:")


@router.message(StateFilter(AgentFlow.registration_name), F.text)
async def registration_name(message: Message, state: FSMContext) -> None:
    name = (message.text or "").strip()
    if not name or len(name) > 255:
        await message.answer("Введите имя (до 255 символов):")
        return
    await state.update_data(agent_name=name)
    await state.set_state(AgentFlow.registration_phone)
    await message.answer("Введите ваш телефон для связи:")


@router.message(StateFilter(AgentFlow.registration_phone), F.text)
async def registration_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip()
    if len(phone) < 5 or len(phone) > 64:
        await message.answer("Введите корректный телефон (5-64 символа):")
        return
    data = await state.get_data()
    async with async_session_factory() as session:
        existing = await _get_agent(session, message.from_user.id)
        if existing is None:
            agent = Agent(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                name=data.get("agent_name", message.from_user.full_name),
                phone=phone,
                language="ru",
                is_active=True,
            )
            session.add(agent)
            await session.commit()
    await state.clear()
    await message.answer(
        "Регистрация завершена. Управляйте листингами через меню ниже.",
        reply_markup=agent_main_menu(),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        agent = await _get_agent(session, message.from_user.id)
    if agent is not None:
        await message.answer("Действие отменено.", reply_markup=agent_main_menu())
    else:
        await message.answer("Действие отменено.")


@router.message(Command("add_listing"))
async def cmd_add_listing(message: Message, state: FSMContext) -> None:
    if await _require_agent(message) is None:
        return
    await state.set_state(AgentFlow.listing_photos)
    await state.update_data(photos=[], price=None, currency=None)
    await _ask_photos(message)


@router.message(StateFilter(AgentFlow.listing_photos), F.photo)
async def listing_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    photos = list(data.get("photos", []))
    photo = message.photo[-1]
    photos.append({"file_id": photo.file_id, "unique_id": photo.file_unique_id})
    await state.update_data(photos=photos)
    await message.answer(
        f"Фото добавлено (всего: {len(photos)}). Ещё фото, /done или /skip."
    )


@router.message(StateFilter(AgentFlow.listing_photos), Command("done"))
@router.message(StateFilter(AgentFlow.listing_photos), Command("skip"))
async def listing_photos_done(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if message.text and message.text.startswith("/skip"):
        await state.update_data(photos=[])
    else:
        photos = data.get("photos", [])
        if not photos:
            await message.answer(
                "Фото пока нет. Отправьте хотя бы одно или используйте /skip."
            )
            return
    await state.set_state(AgentFlow.listing_rooms)
    await message.answer("Сколько комнат? Введите число (0 — студия).")


@router.message(StateFilter(AgentFlow.listing_rooms), F.text)
async def listing_rooms(message: Message, state: FSMContext) -> None:
    rooms = _parse_int(message.text or "")
    if rooms is None or rooms < 0 or rooms > 20:
        await message.answer("Введите количество комнат от 0 до 20 (0 — студия):")
        return
    await state.update_data(rooms=rooms)
    await state.set_state(AgentFlow.listing_price)
    await message.answer(
        "Введите цену и валюту, например: 1200 USD или 30000000 VND."
    )


@router.message(StateFilter(AgentFlow.listing_price), F.text)
async def listing_price(message: Message, state: FSMContext) -> None:
    parsed = _parse_price(message.text or "")
    if parsed is None:
        await message.answer("Не удалось распознать цену. Пример: 1200 USD:")
        return
    price, currency = parsed
    await state.update_data(price=str(price), currency=currency)
    await state.set_state(AgentFlow.listing_district)
    await message.answer("Введите район (например, My Khe или An Thuong):")


@router.message(StateFilter(AgentFlow.listing_district), F.text)
async def listing_district(message: Message, state: FSMContext) -> None:
    district = (message.text or "").strip()
    if not district or len(district) > 255:
        await message.answer("Введите район (до 255 символов):")
        return
    await state.update_data(district=district)
    await state.set_state(AgentFlow.listing_pets)
    await message.answer("Можно ли с питомцами?", reply_markup=pets_keyboard())


@router.callback_query(StateFilter(AgentFlow.listing_pets), F.data.startswith("pets:"))
async def listing_pets(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 1)[1]
    pets_allowed = {"yes": True, "no": False, "any": None}[value]
    await state.update_data(pets_allowed=pets_allowed)
    await state.set_state(AgentFlow.listing_description)
    await callback.answer()
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(f"Питомцы: {_pets_label(pets_allowed)}")
        except Exception:
            pass
    await callback.message.answer("Добавьте описание или /skip:")


@router.message(StateFilter(AgentFlow.listing_description), Command("skip"))
async def listing_description_skip(message: Message, state: FSMContext) -> None:
    await state.update_data(description=None)
    await _show_listing_summary(message, state)


@router.message(StateFilter(AgentFlow.listing_description), F.text)
async def listing_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    await state.update_data(description=description or None)
    await _show_listing_summary(message, state)


async def _show_listing_summary(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(AgentFlow.listing_confirm)
    text = (
        "<b>Проверьте листинг</b>\n"
        f"Фото: {len(data.get('photos', []))}\n"
        f"Комнат: {_rooms_label(data.get('rooms'))}\n"
        f"Цена: {format_price(Decimal(data['price']), data.get('currency', 'USD'))}\n"
        f"Район: {html.escape(data.get('district', ''))}\n"
        f"Питомцы: {_pets_label(data.get('pets_allowed'))}\n"
        f"Описание: {html.escape(data.get('description') or '—')}"
    )
    await message.answer(text, reply_markup=confirm_listing_keyboard())


@router.callback_query(StateFilter(AgentFlow.listing_confirm), F.data == "listing:cancel")
async def listing_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text("Создание листинга отменено.")
        except Exception:
            pass
    await callback.message.answer("Главное меню.", reply_markup=agent_main_menu())


@router.callback_query(StateFilter(AgentFlow.listing_confirm), F.data == "listing:confirm")
async def listing_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    async with async_session_factory() as session:
        agent = await _get_agent(session, callback.from_user.id)
        if agent is None:
            await callback.answer("Сначала /agent_start", show_alert=True)
            await state.clear()
            return

        rooms = int(data.get("rooms") or 0)
        district = data.get("district") or "Da Nang"
        listing = Listing(
            agent_id=agent.id,
            title=_listing_title(rooms, district),
            description=data.get("description"),
            rooms=rooms,
            price=Decimal(data["price"]),
            currency=data.get("currency") or "USD",
            district=district,
            pets_allowed=data.get("pets_allowed"),
            amenities=[],
            is_active=True,
        )
        session.add(listing)
        await session.flush()
        for position, photo in enumerate(data.get("photos", [])):
            session.add(
                ListingPhoto(
                    listing_id=listing.id,
                    telegram_file_id=photo["file_id"],
                    file_unique_id=photo.get("unique_id"),
                    position=position,
                )
            )
        await session.commit()
        listing_id = listing.id

    await state.clear()
    await callback.answer("Сохранено")
    agent_listings = await _get_agent_listings(agent.id)
    listing_number = _listing_display_number(agent_listings, listing_id)
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(f"Листинг #{listing_number} сохранён.")
        except Exception:
            pass
    await callback.message.answer(
        f"Листинг #{listing_number} сохранён.", reply_markup=agent_main_menu()
    )


async def _send_my_listings(message: Message, agent_id: int) -> None:
    listings = await _get_agent_listings(agent_id)
    if not listings:
        await message.answer("У вас пока нет листингов. Добавьте: /add_listing")
        return
    for listing_number, listing in enumerate(listings, start=1):
        status = "активен" if listing.is_active else "деактивирован"
        text = (
            f"#{listing_number} {html.escape(listing.title)}\n"
            f"Комнат: {_rooms_label(listing.rooms)}\n"
            f"Цена: {format_price(listing.price, listing.currency)}\n"
            f"Район: {html.escape(listing.district or '—')}\n"
            f"Статус: {status}"
        )
        await message.answer(
            text,
            reply_markup=listing_actions_keyboard(listing.id, listing.is_active),
        )


async def _show_my_listings(message: Message) -> None:
    async with async_session_factory() as session:
        agent = await _get_agent(session, message.from_user.id)
        if agent is None:
            await message.answer("Сначала зарегистрируйтесь: /agent_start")
            return
        await _send_my_listings(message, agent.id)


@router.message(Command("my_listings"))
async def cmd_my_listings(message: Message) -> None:
    await _show_my_listings(message)


@router.callback_query(F.data.startswith("listing:toggle:"))
async def listing_toggle(callback: CallbackQuery) -> None:
    listing_id = int(callback.data.rsplit(":", 1)[1])
    async with async_session_factory() as session:
        agent = await _get_agent(session, callback.from_user.id)
        if agent is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        listing = (
            await session.execute(
                select(Listing).where(
                    Listing.id == listing_id, Listing.agent_id == agent.id
                )
            )
        ).scalar_one_or_none()
        if listing is None:
            await callback.answer("Листинг не найден", show_alert=True)
            return
        listing.is_active = not listing.is_active
        is_active = listing.is_active
        title = listing.title
        rooms = listing.rooms
        price = listing.price
        currency = listing.currency
        district = listing.district
        await session.commit()

    await callback.answer("Статус обновлён")
    if isinstance(callback.message, Message):
        agent_listings = await _get_agent_listings(agent.id)
        listing_number = _listing_display_number(agent_listings, listing.id)
        status = "активен" if is_active else "деактивирован"
        text = (
            f"#{listing_number} {html.escape(title)}\n"
            f"Комнат: {_rooms_label(rooms)}\n"
            f"Цена: {format_price(price, currency)}\n"
            f"Район: {html.escape(district or '—')}\n"
            f"Статус: {status}"
        )
        try:
            await callback.message.edit_text(
                text, reply_markup=listing_actions_keyboard(listing_id, is_active)
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith("listing:delete:"))
async def listing_delete(callback: CallbackQuery) -> None:
    listing_id = int(callback.data.rsplit(":", 1)[1])
    async with async_session_factory() as session:
        agent = await _get_agent(session, callback.from_user.id)
        if agent is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        listing = (
            await session.execute(
                select(Listing).where(
                    Listing.id == listing_id, Listing.agent_id == agent.id
                )
            )
        ).scalar_one_or_none()
        if listing is None:
            await callback.answer("Листинг не найден", show_alert=True)
            return
        await session.delete(listing)
        await session.commit()

    await callback.answer("Удалено")
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(f"Листинг #{listing_id} удалён.")
        except Exception:
            pass
        await _send_my_listings(callback.message, agent.id)


async def _show_my_leads(message: Message) -> None:
    async with async_session_factory() as session:
        agent = await _get_agent(session, message.from_user.id)
        if agent is None:
            await message.answer("Сначала зарегистрируйтесь: /agent_start")
            return
        stmt = (
            select(Lead)
            .options(selectinload(Lead.client))
            .where(Lead.agent_id == agent.id)
            .order_by(Lead.created_at.desc())
            .limit(10)
        )
        leads = list((await session.execute(stmt)).scalars().all())

    if not leads:
        await message.answer("Лидов пока нет.")
        return

    for lead in leads:
        client_name = lead.client.first_name or "Клиент"
        username = f" (@{lead.client.username})" if lead.client.username else ""
        text = (
            f"<b>Лид #{lead.id}</b> — {_LEAD_STATUS_LABELS.get(lead.status, lead.status)}\n"
            f"Клиент: {html.escape(client_name)}{html.escape(username)}\n"
            f"Язык: {lead.original_language}\n"
            f"Сообщение: {html.escape(lead.original_message[:400])}\n\n"
            f"<b>Слоты</b>\n{html.escape(format_slots(lead.slots))}\n"
            f"Создан: {lead.created_at:%d.%m.%Y %H:%M}"
        )
        await message.answer(text)


@router.message(Command("my_leads"))
async def cmd_my_leads(message: Message) -> None:
    await _show_my_leads(message)


@router.callback_query(F.data.startswith("client_contact:"))
async def client_contact(callback: CallbackQuery) -> None:
    client_id = int(callback.data.rsplit(":", 1)[1])
    async with async_session_factory() as session:
        agent = await _get_agent(session, callback.from_user.id)
        if agent is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        client = (
            await session.execute(select(Client).where(Client.id == client_id))
        ).scalar_one_or_none()
        if client is None:
            await callback.answer("Клиент не найден", show_alert=True)
            return
        await callback.answer(
            f"Telegram ID клиента: {client.telegram_id}\n"
            f"Ссылка: tg://user?id={client.telegram_id}",
            show_alert=True,
        )


def _parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    text_value = str(value).strip().lower()
    if not text_value:
        return None
    if text_value in {"1", "true", "yes", "y", "ok", "o", "да"}:
        return True
    if text_value in {"0", "false", "no", "n", "нет"}:
        return False
    return None


async def _import_listing_photo(
    bot: Any, session: AsyncSession, agent: Agent, listing: Listing, url: str, position: int
) -> tuple[bool, str]:
    async with AsyncClient(timeout=20.0, follow_redirects=True) as client:
        try:
            response = await client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    )
                },
            )
        except Exception as exc:
            return False, f"не удалось скачать фото: {exc}"
        if response.status_code != 200:
            return False, f"не удалось скачать фото (http {response.status_code})"
        content_type = response.headers.get("content-type") or ""
        if "image" not in content_type:
            return False, f"Ссылка не является картинкой: {url}"
        if len(response.content) > 10 * 1024 * 1024:
            return False, "фото слишком велико"
        telegram_photo = BufferedInputFile(file=response.content, filename=f"photo_{position}.jpg")
        try:
            sent_photo = await bot.send_photo(chat_id=agent.telegram_id, photo=telegram_photo)
        except Exception as exc:
            return False, f"не удалось отправить фото в Telegram: {exc}"
        file_id = sent_photo.photo[-1].file_id
        session.add(
            ListingPhoto(
                listing_id=listing.id,
                telegram_file_id=file_id,
                position=position,
            )
        )
        return True, "ok"


@router.message(Command("import_excel"))
async def cmd_import_excel(message: Message, state: FSMContext) -> None:
    if await _require_agent(message) is None:
        return
    await state.set_state(AgentFlow.import_excel)
    await message.answer("Отправьте Excel-файл (.xlsx) со списком листингов.")


@router.callback_query(F.data == "menu:import")
async def menu_import(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session_factory() as session:
        agent = await _get_agent(session, callback.from_user.id)
    if agent is None:
        await callback.answer("Сначала /agent_start", show_alert=True)
        return
    await state.set_state(AgentFlow.import_excel)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer("Отправьте Excel-файл (.xlsx) со списком листингов.")


@router.callback_query(F.data == "menu:add_listing")
async def menu_add_listing(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session_factory() as session:
        agent = await _get_agent(session, callback.from_user.id)
    if agent is None:
        await callback.answer("Сначала /agent_start", show_alert=True)
        return
    await state.set_state(AgentFlow.listing_photos)
    await state.update_data(photos=[], price=None, currency=None)
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "Отправьте фотографии листинга (можно несколько).\n"
            "Когда закончите — /done. Чтобы пропустить — /skip."
        )


@router.callback_query(F.data == "menu:my_listings")
async def menu_my_listings(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_my_listings(callback.message)


@router.callback_query(F.data == "menu:my_leads")
async def menu_my_leads(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await _show_my_leads(callback.message)


@router.message(lambda message: bool(message.text) and message.text in _AGENT_MENU_ACTIONS)
async def agent_menu_button(message: Message, state: FSMContext) -> None:
    if (await state.get_state()) is not None:
        return
    action = _AGENT_MENU_ACTIONS[message.text or ""]
    if action == "help":
        if await _require_agent(message) is None:
            return
        await message.answer(
            "Вы зарегистрированы как риелтор. Ваши команды:\n"
            "/add_listing — добавить листинг\n"
            "/my_listings — мои листинги\n"
            "/my_leads — мои лиды\n"
            "/import_excel — загрузить базу из Excel"
        )
        return
    if action == "import":
        if await _require_agent(message) is None:
            return
        await state.set_state(AgentFlow.import_excel)
        await message.answer("Отправьте Excel-файл (.xlsx) со списком листингов.")
        return
    if action == "add_listing":
        if await _require_agent(message) is None:
            return
        await state.set_state(AgentFlow.listing_photos)
        await state.update_data(photos=[], price=None, currency=None)
        await _ask_photos(message)
        return
    if action == "my_listings":
        await _show_my_listings(message)
        return
    if action == "my_leads":
        await _show_my_leads(message)
        return


@router.message(StateFilter(AgentFlow.import_excel), F.document)
async def import_excel_document(message: Message, state: FSMContext) -> None:
    document = message.document
    if document is None or not (document.file_name or "").lower().endswith(".xlsx"):
        await message.answer("Пожалуйста, отправьте файл в формате .xlsx")
        return

    async with async_session_factory() as session:
        agent = await _get_agent(session, message.from_user.id)
        if agent is None:
            await message.answer("Сначала /agent_start.")
            await state.clear()
            return

        job = ImportJob(
            agent_id=agent.id,
            status="running",
            source="excel",
            total_items=0,
            processed_items=0,
            failed_items=0,
        )
        session.add(job)
        await session.flush()
        job_id = job.id

        telegram_file = await message.bot.get_file(document.file_id)
        file_content = await message.bot.download_file(telegram_file.file_path)
        workbook = load_workbook(
            filename=io.BytesIO(file_content.getvalue() if file_content else b""),
            read_only=True,
            data_only=True,
        )
        processed = 0
        failed = 0
        errors: list[str] = []

        try:
            if not workbook.worksheets:
                raise ValueError("workbook has no sheets")
            worksheet = workbook.worksheets[0]
            rows = [list(row) for row in worksheet.iter_rows(values_only=True)]
            headers: dict[str, int] = {}
            for index, value in enumerate(rows[0] if rows else []):
                if value is None:
                    continue
                headers[str(value).strip().lower()] = index

            def cell_value(row: tuple, name: str) -> Any:
                index = headers.get(name)
                if index is None or index >= len(row):
                    return None
                return row[index]

            for row_index, row in enumerate(rows[1:], start=2):
                try:
                    payload: dict[str, Any] = {
                        "row": row_index,
                        "title": cell_value(row, "title"),
                        "rooms": cell_value(row, "rooms"),
                        "price": cell_value(row, "price"),
                        "currency": cell_value(row, "currency"),
                        "district": cell_value(row, "district"),
                        "pets_allowed": cell_value(row, "pets_allowed"),
                        "photo_urls": cell_value(row, "photo_urls"),
                    }
                    title = str(cell_value(row, "title") or "").strip()
                    price_raw = cell_value(row, "price")
                    parsed_price = _parse_price(str(price_raw)) if price_raw is not None else None

                    if not title or parsed_price is None:
                        error_text = f"row {row_index}: missing title or price"
                        session.add(
                            ImportError(
                                job_id=job.id,
                                row_number=row_index,
                                payload=payload,
                                error=error_text,
                            )
                        )
                        failed += 1
                        errors.append(error_text)
                        continue

                    rooms_raw = cell_value(row, "rooms")
                    rooms = _parse_int(str(rooms_raw).strip()) if rooms_raw is not None else None
                    currency_raw = str(cell_value(row, "currency") or "USD").strip().upper() or "USD"
                    district = str(cell_value(row, "district") or "").strip() or None
                    pets_allowed = _parse_bool(cell_value(row, "pets_allowed"))

                    listing = Listing(
                        agent_id=agent.id,
                        title=title,
                        description=title,
                        rooms=rooms if rooms is not None and rooms >= 0 and rooms <= 20 else None,
                        price=parsed_price[0],
                        currency=currency_raw if currency_raw in {"USD", "VND"} else "USD",
                        district=district,
                        pets_allowed=pets_allowed,
                        amenities=[],
                        is_active=True,
                    )
                    session.add(listing)
                    await session.flush()
                    processed += 1

                    photo_urls_raw = str(cell_value(row, "photo_urls") or "")
                    photo_urls = [item.strip() for item in re.split(r"[,;\s]+", photo_urls_raw) if item.strip()]
                    for position, url in enumerate(photo_urls):
                        ok, error_text = await _import_listing_photo(
                            message.bot, session, agent, listing, url, position
                        )
                        if not ok:
                            failed += 1
                            errors.append(f"row {row_index}: {error_text}")
                            session.add(
                                ImportError(
                                    job_id=job.id,
                                    row_number=row_index,
                                    payload=payload,
                                    error=error_text,
                                )
                            )
                except Exception as row_exc:
                    error_text = f"row {row_index}: {row_exc}"
                    logger.error("Excel import failed at row %s: %s", row_index, row_exc, exc_info=True)
                    failed += 1
                    errors.append(error_text)
                    session.add(
                        ImportError(
                            job_id=job.id,
                            row_number=row_index,
                            error=error_text,
                        )
                    )
        except Exception as exc:
            job.status = "failed"
            job.error_message = f"{type(exc).__name__}: {exc}"
            logger.error("Excel import failed: %s", exc, exc_info=True)
            await session.commit()
            await message.answer(
                f"Импорт завершился с ошибкой.\nУспешно добавлено: {processed} листингов.\n"
                f"Ошибок: {failed + 1}.\n{exc}"
            )
            await state.clear()
            return

        job.status = "done" if failed == 0 else "failed"
        job.processed_items = processed
        job.failed_items = failed
        if failed:
            job.error_message = "; ".join(errors[:5])
        await session.commit()

        error_rows = list(
            (
                await session.execute(
                    select(ImportError)
                    .where(ImportError.job_id == job.id)
                    .order_by(ImportError.id)
                    .limit(5)
                )
            ).scalars()
        )

    error_lines = [
        f"Строка {error.row_number}: {error.error}"
        if error.row_number is not None
        else f"Ошибка: {error.error}"
        for error in error_rows
    ]
    answer_text = (
        f"Импорт завершён.\nУспешно добавлено: {processed} листингов.\nОшибок: {failed}."
    )
    if error_lines:
        answer_text += "\n" + "\n".join(error_lines)
    await message.answer(answer_text)
    await state.clear()


async def _resolve_agent_and_listing(
    telegram_id: int, listing_id: int
) -> tuple[Agent | None, Listing | None]:
    async with async_session_factory() as session:
        agent = await _get_agent(session, telegram_id)
        listing = None
        if agent is not None:
            listing = (
                await session.execute(
                    select(Listing).where(
                        Listing.id == listing_id, Listing.agent_id == agent.id
                    )
                )
            ).scalar_one_or_none()
    return agent, listing


def _listing_card_text(listing: Listing, listing_number: int) -> str:
    status = "активен" if listing.is_active else "деактивирован"
    return (
        f"#{listing_number} {html.escape(listing.title)}\n"
        f"Комнат: {_rooms_label(listing.rooms)}\n"
        f"Цена: {format_price(listing.price, listing.currency)}\n"
        f"Район: {html.escape(listing.district or '—')}\n"
        f"Статус: {status}"
    )


def _listing_caption(listing: Listing) -> str:
    return (
        f"<b>{html.escape(listing.title)}</b>\n"
        f"{_rooms_label(listing.rooms)}\n"
        f"Цена: {format_price(listing.price, listing.currency)}\n"
        f"Район: {html.escape(listing.district or '—')}"
    )[:1024]


@router.callback_query(F.data.startswith("listing:photo:"))
async def listing_photo_callback(callback: CallbackQuery) -> None:
    listing_id = int(callback.data.rsplit(":", 1)[1])
    agent, listing = await _resolve_agent_and_listing(
        callback.from_user.id, listing_id
    )
    if agent is None or listing is None:
        await callback.answer("Листинг не найден", show_alert=True)
        return

    async with async_session_factory() as session:
        photos = list(
            (
                await session.execute(
                    select(ListingPhoto).where(
                        ListingPhoto.listing_id == listing_id
                    )
                    .order_by(ListingPhoto.position)
                )
            ).scalars()
        )

    if not photos:
        await callback.answer("У листинга пока нет фото", show_alert=True)
        return

    if len(photos) == 1:
        await callback.message.answer_photo(
            photo=photos[0].telegram_file_id,
            caption=_listing_caption(listing),
        )
    elif len(photos) <= 10:
        media = [
            InputMediaPhoto(
                media=photo.telegram_file_id,
                caption=_listing_caption(listing) if index == 0 else None,
            )
            for index, photo in enumerate(photos)
        ]
        await callback.message.answer_media_group(media=media)
    else:
        for index, photo in enumerate(photos):
            if index == 0:
                await callback.message.answer_photo(
                    photo=photo.telegram_file_id, caption=_listing_caption(listing)
                )
            else:
                await callback.message.answer_photo(photo=photo.telegram_file_id)

    full_description = listing.description or "Описание не указано."
    agent_listings = await _get_agent_listings(agent.id)
    listing_number = _listing_display_number(agent_listings, listing.id)
    await callback.message.answer(
        f"<b>Полное описание</b>\n#{listing_number}\n{html.escape(full_description)}"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("listing:edit:"))
async def listing_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    listing_id = int(callback.data.rsplit(":", 1)[1])
    agent, listing = await _resolve_agent_and_listing(
        callback.from_user.id, listing_id
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        if agent is None or listing is None:
            await callback.message.edit_text("Листинг не найден.")
            return
        await state.set_state(AgentFlow.listing_edit)
        await state.update_data(agent_id=agent.id, listing_id=listing_id)
        await callback.message.edit_text(
            "Выберите, что изменить:",
            reply_markup=listing_edit_keyboard(listing_id),
        )


@router.callback_query(F.data.startswith("listing:back:"))
async def listing_edit_back(callback: CallbackQuery, state: FSMContext) -> None:
    listing_id = int(callback.data.rsplit(":", 1)[1])
    agent, listing = await _resolve_agent_and_listing(
        callback.from_user.id, listing_id
    )
    await callback.answer()
    if isinstance(callback.message, Message):
        if agent is None or listing is None:
            await callback.message.edit_text("Листинг не найден.")
            return
        agent_listings = await _get_agent_listings(agent.id)
        listing_number = _listing_display_number(agent_listings, listing.id)
        await callback.message.edit_text(
            _listing_card_text(listing, listing_number),
            reply_markup=listing_actions_keyboard(listing.id, listing.is_active),
        )


@router.callback_query(F.data.startswith("edit:price:"))
async def edit_price_start(callback: CallbackQuery, state: FSMContext) -> None:
    listing_id = int(callback.data.rsplit(":", 1)[1])
    agent, listing = await _resolve_agent_and_listing(
        callback.from_user.id, listing_id
    )
    if agent is None or listing is None:
        await callback.answer("Листинг не найден", show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        agent_listings = await _get_agent_listings(agent.id)
        listing_number = _listing_display_number(agent_listings, listing.id)
        await state.set_state(AgentFlow.listing_edit_price)
        await state.update_data(agent_id=agent.id, listing_id=listing_id)
        await callback.message.edit_text(
            f"Отправьте новую цену для листинга #{listing_number} (например: 1200 USD):",
        )


@router.callback_query(F.data.startswith("edit:description:"))
async def edit_description_start(callback: CallbackQuery, state: FSMContext) -> None:
    listing_id = int(callback.data.rsplit(":", 1)[1])
    agent, listing = await _resolve_agent_and_listing(
        callback.from_user.id, listing_id
    )
    if agent is None or listing is None:
        await callback.answer("Листинг не найден", show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        agent_listings = await _get_agent_listings(agent.id)
        listing_number = _listing_display_number(agent_listings, listing.id)
        await state.set_state(AgentFlow.listing_edit_description)
        await state.update_data(agent_id=agent.id, listing_id=listing_id)
        await callback.message.edit_text(
            f"Отправьте новое описание для листинга #{listing_number}:",
        )


@router.callback_query(F.data.startswith("edit:photos:"))
async def edit_photos_start(callback: CallbackQuery, state: FSMContext) -> None:
    listing_id = int(callback.data.rsplit(":", 1)[1])
    agent, listing = await _resolve_agent_and_listing(
        callback.from_user.id, listing_id
    )
    if agent is None or listing is None:
        await callback.answer("Листинг не найден", show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        agent_listings = await _get_agent_listings(agent.id)
        listing_number = _listing_display_number(agent_listings, listing.id)
        await state.set_state(AgentFlow.listing_add_photos)
        await state.update_data(agent_id=agent.id, listing_id=listing_id)
        await callback.message.edit_text(
            f"Отправьте новые фото для листинга #{listing_number} (можно несколько).",
        )


@router.callback_query(F.data.startswith("edit:delete_photos:"))
async def edit_delete_photos(callback: CallbackQuery, state: FSMContext) -> None:
    listing_id = int(callback.data.rsplit(":", 1)[1])
    agent, listing = await _resolve_agent_and_listing(
        callback.from_user.id, listing_id
    )
    if agent is None or listing is None:
        await callback.answer("Листинг не найден", show_alert=True)
        return
    async with async_session_factory() as session:
        await session.execute(
            delete(ListingPhoto).where(ListingPhoto.listing_id == listing_id)
        )
        await session.commit()
    if isinstance(callback.message, Message):
        await callback.message.edit_text("Фото листинга удалены.")
    await state.set_state(AgentFlow.listing_edit)
    await state.update_data(agent_id=agent.id, listing_id=listing_id)
    await callback.answer()


@router.callback_query(F.data.startswith("edit:status:"))
async def edit_status(callback: CallbackQuery) -> None:
    listing_id = int(callback.data.rsplit(":", 1)[1])
    async with async_session_factory() as session:
        agent = await _get_agent(session, callback.from_user.id)
        listing = None
        if agent is not None:
            listing = (
                await session.execute(
                    select(Listing).where(
                        Listing.id == listing_id, Listing.agent_id == agent.id
                    )
                )
            ).scalar_one_or_none()
        if agent is None or listing is None:
            await callback.answer("Листинг не найден", show_alert=True)
            return
        listing.is_active = not listing.is_active
        await session.commit()

    await callback.answer("Статус обновлён")
    if isinstance(callback.message, Message):
        agent_listings = await _get_agent_listings(agent.id)
        listing_number = _listing_display_number(agent_listings, listing.id)
        await callback.message.edit_text(
            _listing_card_text(listing, listing_number),
            reply_markup=listing_actions_keyboard(listing.id, listing.is_active),
        )


@router.message(StateFilter(AgentFlow.listing_edit_price), F.text)
async def listing_edit_price(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    agent_id = data.get("agent_id")
    listing_id = data.get("listing_id")
    if not isinstance(agent_id, int) or not isinstance(listing_id, int):
        await state.clear()
        await message.answer(
            "Процесс редактирования не найден. Начните через /add_listing или /my_listings."
        )
        return

    async with async_session_factory() as session:
        agent = await _get_agent(session, message.from_user.id)
        listing = None
        if agent is not None:
            listing = (
                await session.execute(
                    select(Listing).where(
                        Listing.id == listing_id, Listing.agent_id == agent.id
                    )
                )
            ).scalar_one_or_none()
        if listing is None:
            await message.answer("Листинг не найден.")
            await state.clear()
            return
        parsed = _parse_price(message.text or "")
        if parsed is None:
            await message.answer("Не удалось распознать цену. Пример: 1200 USD:")
            return
        listing.price = parsed[0]
        listing.currency = parsed[1]
        await session.commit()

    await state.set_state(AgentFlow.listing_edit)
    await state.update_data(agent_id=agent_id, listing_id=listing_id)
    agent_listings = await _get_agent_listings(agent.id)
    listing_number = _listing_display_number(agent_listings, listing.id)
    await message.answer(f"Цена листинга #{listing_number} обновлена.")


@router.message(StateFilter(AgentFlow.listing_edit_description), F.text)
async def listing_edit_description(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    agent_id = data.get("agent_id")
    listing_id = data.get("listing_id")
    if not isinstance(agent_id, int) or not isinstance(listing_id, int):
        await state.clear()
        await message.answer(
            "Процесс редактирования не найден. Начните через /add_listing или /my_listings."
        )
        return

    async with async_session_factory() as session:
        agent = await _get_agent(session, message.from_user.id)
        listing = None
        if agent is not None:
            listing = (
                await session.execute(
                    select(Listing).where(
                        Listing.id == listing_id, Listing.agent_id == agent.id
                    )
                )
            ).scalar_one_or_none()
        if listing is None:
            await message.answer("Листинг не найден.")
            await state.clear()
            return
        listing.description = (message.text or "").strip() or None
        await session.commit()

    await state.set_state(AgentFlow.listing_edit)
    await state.update_data(agent_id=agent_id, listing_id=listing_id)
    agent_listings = await _get_agent_listings(agent.id)
    listing_number = _listing_display_number(agent_listings, listing.id)
    await message.answer(f"Описание листинга #{listing_number} обновлено.")


@router.message(StateFilter(AgentFlow.listing_add_photos), F.photo)
async def listing_add_photos(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    agent_id = data.get("agent_id")
    listing_id = data.get("listing_id")
    if not isinstance(agent_id, int) or not isinstance(listing_id, int):
        await state.clear()
        await message.answer(
            "Процесс редактирования не найден. Начните через /add_listing или /my_listings."
        )
        return

    async with async_session_factory() as session:
        agent = await _get_agent(session, message.from_user.id)
        listing = None
        if agent is not None:
            listing = (
                await session.execute(
                    select(Listing).where(
                        Listing.id == listing_id, Listing.agent_id == agent.id
                    )
                )
            ).scalar_one_or_none()
        if listing is None:
            await message.answer("Листинг не найден.")
            await state.clear()
            return
        photos_stmt = (
            select(ListingPhoto)
            .where(ListingPhoto.listing_id == listing_id)
            .order_by(ListingPhoto.position)
        )
        existing = list((await session.execute(photos_stmt)).scalars())
        next_position = max((photo.position for photo in existing), default=-1) + 1
        photo = message.photo[-1]
        session.add(
            ListingPhoto(
                listing_id=listing_id,
                telegram_file_id=photo.file_id,
                file_unique_id=photo.file_unique_id,
                position=next_position,
            )
        )
        await session.commit()

    await state.set_state(AgentFlow.listing_edit)
    await state.update_data(agent_id=agent_id, listing_id=listing_id)
    await message.answer("Новое фото добавлено.")
