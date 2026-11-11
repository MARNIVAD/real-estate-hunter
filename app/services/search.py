from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Listing

logger = logging.getLogger(__name__)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price_in_usd_expression():
    # TODO: use a live FX rate or store normalized USD price on the listing.
    return case(
        (Listing.currency == "USD", Listing.price),
        else_=Listing.price / settings.USD_VND_RATE,
    )


async def find_listings(
    session: AsyncSession,
    agent_id: int,
    slots: dict[str, Any],
    limit: int = 5,
) -> list[Listing]:
    """Find active listings of an agent matching non-empty slots."""
    price_usd = _price_in_usd_expression()
    stmt = (
        select(Listing)
        .options(selectinload(Listing.photos))
        .where(Listing.agent_id == agent_id, Listing.is_active.is_(True))
    )

    rooms = slots.get("rooms")
    if rooms is not None:
        stmt = stmt.where(Listing.rooms == int(rooms))

    budget_max = _as_float(slots.get("budget_max"))
    if budget_max is not None:
        currency = slots.get("budget_currency") or "USD"
        budget_usd = budget_max if currency == "USD" else budget_max / settings.USD_VND_RATE
        stmt = stmt.where(Listing.price.is_not(None), price_usd <= budget_usd)

    district = slots.get("district")
    if district:
        stmt = stmt.where(Listing.district.ilike(f"%{district}%"))

    pets_allowed = slots.get("pets_allowed")
    if pets_allowed is not None:
        stmt = stmt.where(Listing.pets_allowed.is_(bool(pets_allowed)))

    area_min = _as_float(slots.get("area_m2_min"))
    if area_min is not None:
        stmt = stmt.where(Listing.area_m2.is_not(None), Listing.area_m2 >= area_min)

    amenities = slots.get("amenities")
    if amenities:
        stmt = stmt.where(Listing.amenities.contains(list(amenities)))

    stmt = stmt.order_by(price_usd.asc().nulls_last(), Listing.id.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def find_listings_fallback(
    session: AsyncSession,
    agent_id: int,
    slots: dict[str, Any],
    limit: int = 3,
) -> dict[str, Any]:
    """Progressively relax filters when the exact search returns nothing."""
    relaxed: list[str] = []

    relaxed_slots = dict(slots)
    budget_max = _as_float(relaxed_slots.get("budget_max"))
    if budget_max is not None:
        relaxed_slots["budget_max"] = int(round(budget_max * 1.2))
        relaxed.append("budget +20%")

    listings = await find_listings(session, agent_id, relaxed_slots, limit=limit)
    if listings:
        return {"listings": listings, "relaxed_what": ", ".join(relaxed)}

    if relaxed_slots.get("district"):
        relaxed_slots["district"] = None
        relaxed.append("district removed")
        listings = await find_listings(session, agent_id, relaxed_slots, limit=limit)
        if listings:
            return {"listings": listings, "relaxed_what": ", ".join(relaxed)}

    if relaxed_slots.get("area_m2_min") is not None:
        relaxed_slots["area_m2_min"] = None
        relaxed.append("min area removed")
        listings = await find_listings(session, agent_id, relaxed_slots, limit=limit)
        if listings:
            return {"listings": listings, "relaxed_what": ", ".join(relaxed)}

    logger.info("Fallback search found no listings for agent_id=%s", agent_id)
    return {"listings": [], "relaxed_what": ", ".join(relaxed)}
