from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📥 Импорт Excel")],
            [KeyboardButton(text="➕ Добавить квартиру")],
            [KeyboardButton(text="📋 Мои квартиры")],
            [KeyboardButton(text="👥 Мои лиды")],
            [KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def agent_main_menu() -> ReplyKeyboardMarkup:
    return get_main_menu_keyboard()


def pets_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Можно с питомцами", callback_data="pets:yes"),
                InlineKeyboardButton(text="Без питомцев", callback_data="pets:no"),
            ],
            [InlineKeyboardButton(text="Неважно", callback_data="pets:any")],
        ]
    )


def confirm_listing_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сохранить", callback_data="listing:confirm"),
                InlineKeyboardButton(text="Отменить", callback_data="listing:cancel"),
            ]
        ]
    )


def listing_actions_keyboard(listing_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle_text = "Деактивировать" if is_active else "Активировать"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=toggle_text, callback_data=f"listing:toggle:{listing_id}"
                ),
                InlineKeyboardButton(
                    text="Удалить", callback_data=f"listing:delete:{listing_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📸 Фото", callback_data=f"listing:photo:{listing_id}"
                ),
                InlineKeyboardButton(
                    text="✏️ Редактировать", callback_data=f"listing:edit:{listing_id}"
                ),
            ],
        ]
    )


def listing_edit_keyboard(listing_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Изменить цену", callback_data=f"edit:price:{listing_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Изменить описание", callback_data=f"edit:description:{listing_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Добавить фото", callback_data=f"edit:photos:{listing_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Удалить все фото", callback_data=f"edit:delete_photos:{listing_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Изменить статус", callback_data=f"edit:status:{listing_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Назад", callback_data=f"listing:back:{listing_id}"
                ),
            ],
        ]
    )


def contact_client_keyboard(username: str | None, client_id: int) -> InlineKeyboardMarkup:
    if username:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Связаться с клиентом", url=f"https://t.me/{username}"
                    )
                ]
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Связаться с клиентом", callback_data=f"client_contact:{client_id}"
                )
            ]
        ]
    )
