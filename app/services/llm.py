from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from decimal import Decimal
from typing import Any

from openai import AsyncOpenAI

from app.config import settings
from app.db import async_session_factory
from app.models import LlmCall

logger = logging.getLogger(__name__)

_INPUT_COST_PER_MILLION = 0.15
_OUTPUT_COST_PER_MILLION = 0.60
_MAX_ATTEMPTS = 3  # initial call + 2 retries

_DISTRICTS = [
    "My Khe",
    "An Thuong",
    "Son Tra",
    "Hai Chau",
    "Cam Le",
    "Ngu Hanh Son",
    "Lien Chieu",
    "Thanh Khe",
    "Hoi An",
]

_AMENITY_PATTERNS: dict[str, tuple[str, ...]] = {
    "pool": ("pool", "бассейн", "hồ bơi"),
    "gym": ("gym", "спортзал", "phòng gym"),
    "parking": ("parking", "парковка", "đỗ xe", "bãi xe"),
    "balcony": ("balcony", "балкон", "ban công"),
    "sea_view": ("sea view", "вид на море", "biển"),
    "elevator": ("elevator", "лифт", "thang máy"),
    "furnished": ("furnished", "мебель", "nội thất"),
    "washing_machine": ("washing machine", "стиральная машина", "máy giặt"),
    "air_conditioning": ("air conditioning", "кондиционер", "điều hòa"),
}

_EXTRACTION_SYSTEM_PROMPT = """You are an assistant for a real estate agency in Da Nang, Vietnam.
Your job is to extract apartment search parameters from a client's free-form message.
The client may write in Russian, English, or Vietnamese.

Return ONLY a valid JSON object with this exact shape:
{
  "language": "ru|en|vi",
  "slots": {
    "rooms": int | null,
    "budget_max": int | null,
    "budget_currency": "USD|VND" | null,
    "area_m2_min": int | null,
    "district": str | null,
    "pets_allowed": bool | null,
    "amenities": [str] | null
  },
  "missing": ["rooms", "budget_max"],
  "next_question": str | null,
  "is_complete": bool
}

Extraction rules:
- rooms: number of bedrooms; 0 means studio; use null when the message does not mention it.
- budget_max: integer amount without separators; use null when no budget is mentioned.
- budget_currency: "USD" or "VND"; infer it from the message. Default to "USD" when a budget is present but currency is unclear.
- area_m2_min: minimum area in square meters as an integer; null if absent.
- district: canonical English transliteration of the Da Nang district/area (for example "My Khe", "An Thuong", "Son Tra", "Hai Chau", "Ngu Hanh Son").
- pets_allowed: true if the client needs a pet-friendly place, false if pets are not allowed/needed, null if unknown.
- amenities: lowercase English amenity names from this list when mentioned: pool, gym, parking, balcony, sea_view, elevator, furnished, washing_machine, air_conditioning. Use null if none are mentioned.
- Never guess. If a value is unclear, use null.
- Critical slots are rooms and budget_max. Set is_complete to true only when both are present.
- missing must list the missing critical slots, in the order ["rooms", "budget_max"].
- next_question must be a short question in the client's language asking about exactly one missing critical slot. Use null when is_complete is true.
- Do not add commentary, markdown, or keys outside the JSON object."""

_TRANSLATION_SYSTEM_PROMPT = """You translate real estate client messages into Russian.
Translate faithfully, keep names, addresses, prices, and numbers unchanged.
Do not add explanations or commentary. Return only the translated text."""


class LlmError(RuntimeError):
    pass


_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI | None:
    global _client
    if not settings.OPENAI_API_KEY:
        return None
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


def _detect_language(text: str) -> str:
    if re.search(r"[А-Яа-яЁё]", text):
        return "ru"
    if re.search(r"[ăâđêôơưĂÂĐÊÔƠƯ]", text, re.IGNORECASE) or re.search(
        r"\b(phòng|căn hộ|giá|cho thuê|thuê)\b", text, re.IGNORECASE
    ):
        return "vi"
    return "en"


def _parse_number(raw: str) -> int | None:
    cleaned = re.sub(r"[^\d]", "", raw)
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _question_for(slot: str, language: str) -> str:
    questions = {
        "ru": {
            "rooms": "Сколько комнат вам нужно? (0 — студия)",
            "budget_max": "Какой у вас максимальный бюджет?",
        },
        "en": {
            "rooms": "How many bedrooms do you need? (0 for a studio)",
            "budget_max": "What is your maximum budget?",
        },
        "vi": {
            "rooms": "Bạn cần bao nhiêu phòng ngủ? (0 là căn hộ studio)",
            "budget_max": "Ngân sách tối đa của bạn là bao nhiêu?",
        },
    }
    return questions.get(language, questions["en"]).get(slot, "Could you clarify your request?")


def _normalize_result(payload: dict[str, Any], fallback_language: str) -> dict[str, Any]:
    language = payload.get("language")
    if language not in {"ru", "en", "vi"}:
        language = fallback_language

    raw_slots = payload.get("slots")
    if not isinstance(raw_slots, dict):
        raw_slots = {}

    def as_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    def as_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        return None

    amenities = raw_slots.get("amenities")
    if isinstance(amenities, str):
        amenities = [amenities]
    if not isinstance(amenities, list):
        amenities = None
    else:
        amenities = [str(item).strip().lower() for item in amenities if str(item).strip()] or None

    currency = raw_slots.get("budget_currency")
    if currency not in {"USD", "VND"}:
        currency = None

    slots = {
        "rooms": as_int(raw_slots.get("rooms")),
        "budget_max": as_int(raw_slots.get("budget_max")),
        "budget_currency": currency,
        "area_m2_min": as_int(raw_slots.get("area_m2_min")),
        "district": (str(raw_slots["district"]).strip() if raw_slots.get("district") else None),
        "pets_allowed": as_bool(raw_slots.get("pets_allowed")),
        "amenities": amenities,
    }
    if slots["budget_max"] is not None and slots["budget_currency"] is None:
        slots["budget_currency"] = "USD"

    missing = [slot for slot in ("rooms", "budget_max") if slots[slot] is None]
    is_complete = not missing

    next_question = payload.get("next_question")
    if is_complete:
        next_question = None
    elif not isinstance(next_question, str) or not next_question.strip():
        next_question = _question_for(missing[0], language)

    return {
        "language": language,
        "slots": slots,
        "missing": missing,
        "next_question": next_question,
        "is_complete": is_complete,
    }


def _mock_extract(text: str, language_hint: str | None = None) -> dict[str, Any]:
    """Deterministic fallback used without an API key or when the LLM fails."""
    language = language_hint if language_hint in {"ru", "en", "vi"} else _detect_language(text)

    rooms: int | None = None
    if re.search(r"\bstudio\b|студи|studi", text, re.IGNORECASE):
        rooms = 0
    else:
        room_match = re.search(
            r"(\d+)\s*(?:-?\s*(?:комнат\w*|bedrooms?|bhk|rooms?|phòng(?:\s+ngủ)?))",
            text,
            re.IGNORECASE,
        )
        if room_match:
            rooms = int(room_match.group(1))

    budget_max: int | None = None
    budget_currency: str | None = None
    budget_match = re.search(
        r"(\d[\d\s.,]*)\s*(usd|доллар\w*|\$|vnd|₫|донг\w*|dong)\b", text, re.IGNORECASE
    )
    if budget_match:
        budget_max = _parse_number(budget_match.group(1))
        token = budget_match.group(2).lower()
        budget_currency = "VND" if token in {"vnd", "₫", "донг", "донгов", "dong"} else "USD"
    else:
        budget_match = re.search(
            r"(?:до|max(?:imum)?|budget|бюджет|under|tối đa)\s*[:=]?\s*(\d[\d\s.,]*)",
            text,
            re.IGNORECASE,
        )
        if budget_match:
            budget_max = _parse_number(budget_match.group(1))
            budget_currency = "USD"
    if budget_max is not None and budget_currency is None:
        budget_currency = "USD"

    area_match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:m2|m²|sqm|кв\.?\s*м|mét vuông)", text, re.IGNORECASE
    )
    area_m2_min = int(float(area_match.group(1).replace(",", "."))) if area_match else None

    district = next((item for item in _DISTRICTS if item.lower() in text.lower()), None)

    pets_allowed: bool | None = None
    if re.search(r"без животных|no pets|không nuôi", text, re.IGNORECASE):
        pets_allowed = False
    elif re.search(r"с собакой|с котом|pets?|pet-friendly|thú cưng|chó|mèo", text, re.IGNORECASE):
        pets_allowed = True

    amenities = [
        name
        for name, patterns in _AMENITY_PATTERNS.items()
        if any(pattern in text.lower() for pattern in patterns)
    ]

    slots = {
        "rooms": rooms,
        "budget_max": budget_max,
        "budget_currency": budget_currency,
        "area_m2_min": area_m2_min,
        "district": district,
        "pets_allowed": pets_allowed,
        "amenities": amenities or None,
    }
    missing = [slot for slot in ("rooms", "budget_max") if slots[slot] is None]
    return {
        "language": language,
        "slots": slots,
        "missing": missing,
        "next_question": _question_for(missing[0], language) if missing else None,
        "is_complete": not missing,
        "_mock": True,
    }


async def _save_llm_call(
    *,
    purpose: str,
    model: str,
    input_text: str,
    output_text: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    latency_ms: int,
    success: bool,
    error: str | None = None,
) -> None:
    try:
        async with async_session_factory() as session:
            session.add(
                LlmCall(
                    purpose=purpose,
                    model=model,
                    input_text=input_text[:8000],
                    output_text=output_text[:8000] if output_text else None,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=Decimal(str(round(cost_usd, 6))),
                    latency_ms=latency_ms,
                    success=success,
                    error=(error[:2000] if error else None),
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Failed to persist llm_calls row")


async def _chat_json(messages: list[dict[str, str]], purpose: str) -> dict[str, Any]:
    client = _get_client()
    if client is None:
        raise LlmError("OPENAI_API_KEY is not configured")

    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        started = time.perf_counter()
        try:
            response = await client.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=messages,  # type: ignore[arg-type]
                response_format={"type": "json_object"},
                temperature=0,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            content = response.choices[0].message.content or ""
            tokens_in = int(response.usage.prompt_tokens) if response.usage else 0
            tokens_out = int(response.usage.completion_tokens) if response.usage else 0
            cost = (
                tokens_in / 1_000_000 * _INPUT_COST_PER_MILLION
                + tokens_out / 1_000_000 * _OUTPUT_COST_PER_MILLION
            )
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                await _save_llm_call(
                    purpose=purpose,
                    model=settings.OPENAI_MODEL,
                    input_text=messages[-1]["content"],
                    output_text=content,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    success=False,
                    error=f"JSONDecodeError: {exc}",
                )
                last_error = exc
                logger.warning("LLM returned invalid JSON (attempt %s/%s)", attempt, _MAX_ATTEMPTS)
                await asyncio.sleep(0.4 * attempt)
                continue

            await _save_llm_call(
                purpose=purpose,
                model=settings.OPENAI_MODEL,
                input_text=messages[-1]["content"],
                output_text=content,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
                latency_ms=latency_ms,
                success=True,
            )
            if not isinstance(parsed, dict):
                raise LlmError("LLM JSON response is not an object")
            return parsed
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await _save_llm_call(
                purpose=purpose,
                model=settings.OPENAI_MODEL,
                input_text=messages[-1]["content"],
                output_text="",
                tokens_in=0,
                tokens_out=0,
                cost_usd=0,
                latency_ms=latency_ms,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            last_error = exc
            logger.warning(
                "LLM call failed (attempt %s/%s): %s", attempt, _MAX_ATTEMPTS, exc
            )
            await asyncio.sleep(0.4 * attempt)

    raise LlmError(f"LLM call failed after {_MAX_ATTEMPTS} attempts: {last_error}")


async def extract_slots(text: str, language_hint: str | None = None) -> dict[str, Any]:
    """Extract apartment search slots from a client message.

    The returned dictionary is intentionally normalized: unknown values are
    ``None`` and ``missing`` contains only the critical slots.
    """
    fallback_language = language_hint if language_hint in {"ru", "en", "vi"} else _detect_language(text)

    client = _get_client()
    if client is None:
        logger.info("OPENAI_API_KEY is not set; using mock slot extraction")
        return _mock_extract(text, fallback_language)

    messages = [
        {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Client language hint: {language_hint or 'unknown'}\n"
                f"Client message:\n{text}"
            ),
        },
    ]
    try:
        payload = await _chat_json(messages, purpose="extract_slots")
    except LlmError as exc:
        logger.error("Slot extraction failed, switching to mock: %s", exc)
        return _mock_extract(text, fallback_language)

    return _normalize_result(payload, fallback_language)


async def translate_to_ru(text: str, source_lang: str) -> str:
    """Translate text to Russian. Russian input is returned unchanged."""
    if source_lang == "ru" or not text.strip():
        return text

    client = _get_client()
    if client is None:
        logger.info("OPENAI_API_KEY is not set; translation skipped")
        return text

    started = time.perf_counter()
    messages = [
        {"role": "system", "content": _TRANSLATION_SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    try:
        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=messages,  # type: ignore[arg-type]
            temperature=0,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        content = response.choices[0].message.content or text
        tokens_in = int(response.usage.prompt_tokens) if response.usage else 0
        tokens_out = int(response.usage.completion_tokens) if response.usage else 0
        cost = (
            tokens_in / 1_000_000 * _INPUT_COST_PER_MILLION
            + tokens_out / 1_000_000 * _OUTPUT_COST_PER_MILLION
        )
        await _save_llm_call(
            purpose="translate",
            model=settings.OPENAI_MODEL,
            input_text=text,
            output_text=content,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            latency_ms=latency_ms,
            success=True,
        )
        return content.strip() or text
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        await _save_llm_call(
            purpose="translate",
            model=settings.OPENAI_MODEL,
            input_text=text,
            output_text="",
            tokens_in=0,
            tokens_out=0,
            cost_usd=0,
            latency_ms=latency_ms,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        logger.error("Translation failed: %s", exc)
        return text
