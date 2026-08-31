from handlers import account, admin, catalog, checkout, common, start


def register_all(client) -> None:
    start.register(client)
    catalog.register(client)
    checkout.register(client)
    account.register(client)
    admin.register(client)


__all__ = ["register_all", "account", "admin", "catalog", "checkout", "common", "start"]
