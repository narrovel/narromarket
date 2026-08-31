from handlers.admin import (
    cards,
    catalog,
    menu,
    offers,
    orders,
    people,
    requisites,
    settings_panel,
)
from handlers.admin import input as admin_input


def register(client) -> None:
    menu.register(client)
    orders.register(client)
    catalog.register(client)
    offers.register(client)
    people.register(client)
    requisites.register(client)
    settings_panel.register(client)
    admin_input.register(client)


__all__ = ["cards", "register"]
