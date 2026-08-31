# Step by step flows: which step a user is on and what was entered so far.
#
# The key is the chat plus the user, never the user alone. A flow opened in a private
# chat must not swallow the same person's next message in a group.

import time
from typing import Any, Optional

# How long an opened flow stays armed. Without it a prompt left unanswered would still
# be waiting days later, after the rights that opened it were taken away.
TTL_SECONDS = 30 * 60

_states: dict[tuple, dict[str, Any]] = {}


def event_key(event) -> tuple:
    return (getattr(event, "chat_id", None) or 0, event.sender_id)


def set(chat_id: int, user_id: int, name: str, section: str = None, /, **data: Any) -> None:
    _states[(chat_id, user_id)] = {
        "name": name,
        "section": section,
        "data": data,
        "started": time.monotonic(),
    }


def get(chat_id: int, user_id: int) -> Optional[dict]:
    state = _states.get((chat_id, user_id))
    if state is None:
        return None
    if time.monotonic() - state["started"] > TTL_SECONDS:
        _states.pop((chat_id, user_id), None)
        return None
    return state


def update(chat_id: int, user_id: int, **data: Any) -> None:
    state = get(chat_id, user_id)
    if state:
        state["data"].update(data)


def clear(chat_id: int, user_id: int) -> None:
    _states.pop((chat_id, user_id), None)


def set_for(event, name: str, section: str = None, /, **data: Any) -> None:
    # Every parameter of this function is positional only, and the state is written
    # straight into the store rather than forwarded through set(). Both are there for
    # the same reason: a flow that wants to remember a value under "name", "section" or
    # "user_id" must not collide with the bookkeeping and raise instead of saving it.
    _states[event_key(event)] = {
        "name": name,
        "section": section,
        "actor_user_id": int(getattr(event, "_narromarket_user_id", 0) or 0),
        "data": data,
        "started": time.monotonic(),
    }


def get_for(event) -> Optional[dict]:
    chat_id, user_id = event_key(event)
    return get(chat_id, user_id)


def clear_for(event) -> None:
    chat_id, user_id = event_key(event)
    clear(chat_id, user_id)


def sweep() -> int:
    now = time.monotonic()
    stale = [key for key, state in _states.items() if now - state["started"] > TTL_SECONDS]
    for key in stale:
        _states.pop(key, None)
    return len(stale)
