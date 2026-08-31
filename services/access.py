# Access rules for the admin panel.

from typing import Optional

from db import users as users_db

SECTION_ROLES = {
    "orders": "manager",
    "subscriptions": "manager",
    "stats": "manager",
    "users": "manager",
    "catalog": "admin",
    "offers": "admin",
    "requisites": "admin",
    "settings": "admin",
    "broadcast": "admin",
    "roles": "admin",
}


def level(user: Optional[dict]) -> int:
    return users_db.role_level(user["role"]) if user else 0


def is_staff(user: Optional[dict]) -> bool:
    return level(user) >= users_db.role_level("manager")


def can(user: Optional[dict], section: str) -> bool:
    if not user or user.get("is_blocked"):
        return False
    required = SECTION_ROLES.get(section, "admin")
    return level(user) >= users_db.role_level(required)


def can_manage_user(actor: Optional[dict], target: Optional[dict]) -> bool:
    if not actor or not target or actor["id"] == target["id"]:
        return False
    if target.get("telegram_id", 0) <= 0:
        return False
    if target["role"] == "owner":
        return False
    if actor["role"] == "owner":
        return True
    return level(actor) > level(target)


def can_assign_role(actor: Optional[dict], target: Optional[dict], role: str) -> bool:
    if role not in users_db.ROLES or role == "owner":
        return False
    if not can_manage_user(actor, target):
        return False
    return actor["role"] == "owner" or level(actor) > users_db.role_level(role)


async def actor(telegram_id: int) -> Optional[dict]:
    return await users_db.get(telegram_id)
