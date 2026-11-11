from aiogram.fsm.state import State, StatesGroup


class ClientFlow(StatesGroup):
    waiting_query = State()
    waiting_slot = State()
    processing = State()
    results = State()


class AgentFlow(StatesGroup):
    registration_name = State()
    registration_phone = State()

    listing_photos = State()
    listing_rooms = State()
    listing_price = State()
    listing_district = State()
    listing_pets = State()
    listing_description = State()
    listing_confirm = State()
    listing_edit = State()
    listing_edit_price = State()
    listing_edit_description = State()
    listing_add_photos = State()
    import_excel = State()
