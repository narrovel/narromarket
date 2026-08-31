# End to end walkthrough: python -m tools.walkthrough
#
# Registers the real handlers, then drives them with a fake Telegram client over a
# throwaway database: a customer buys, renews and pays by transfer, staff processes
# orders and goes through every admin section. Nothing touches the network.

import asyncio
import sys
import traceback

from tools import _sandbox  # noqa: F401  imported first, it sets DATABASE_PATH

TEMP_DIR = _sandbox.TEMP_DIR

from telethon import TelegramClient  # noqa: E402
from telethon.sessions import MemorySession  # noqa: E402
from telethon.extensions import html as html_parser  # noqa: E402
from telethon.tl import types  # noqa: E402

import config  # noqa: E402
from db import connection, orders as orders_db  # noqa: E402
from db import products as products_db, requisites as requisites_db  # noqa: E402
from db import settings, subscriptions as subs_db, users as users_db  # noqa: E402
from handlers import checkout, register_all  # noqa: E402
from tools.seed_demo import DEMO_PRODUCTS  # noqa: E402

_sandbox.guard_live_database()
from utils import dates, states  # noqa: E402

OWNER_ID, CUSTOMER_ID = 111, 222

failures = []
steps = []


class Sender:
    def __init__(self, user_id, username, first_name):
        self.id = user_id
        self.username = username
        self.first_name = first_name
        self.last_name = None


def _check_html(text, parse_mode, where):
    # Telethon parses HTML client-side, so the harness rejects malformed or empty text.
    if not text or parse_mode != "html":
        return
    try:
        parsed, _ = html_parser.parse(text)
    except Exception as error:
        failures.append(f"{where}: telegram would reject this html: {error}")
        return
    if not parsed.strip():
        failures.append(f"{where}: html parsed away to nothing: {text[:60]!r}")


class FakeClient:
    # Collects what the bot would send instead of talking to Telegram.
    def __init__(self):
        self.sent = []
        self.sent_buttons = []
        self.requests = []
        self.fail_refunds = 0

    async def send_message(self, peer, text=None, buttons=None, parse_mode=None, file=None):
        _check_html(text, parse_mode, "send_message")
        self.sent.append((peer, text or "<invoice>"))
        self.sent_buttons.append(buttons)
        return object()

    async def send_file(self, peer, path, caption=None, buttons=None, parse_mode=None):
        _check_html(caption, parse_mode, "send_file")
        self.sent.append((peer, caption))
        return object()

    async def __call__(self, request):
        # Both bot RPCs are captured here: refunds and pre-checkout responses.
        self.requests.append(request)
        self.sent.append(("api", type(request).__name__))
        if type(request).__name__ == "RefundStarsChargeRequest" and self.fail_refunds:
            self.fail_refunds -= 1
            raise RuntimeError("temporary refund failure")
        return object()


class FakeMessage:
    def __init__(self, text):
        self.text = text
        # Telethon exposes the raw text as .message; .text is the re parsed variant.
        self.message = text


class FakeEvent:
    def __init__(self, sender, client, data=None, text=None):
        self.sender = sender
        self.sender_id = sender.id
        # Every conversation with the bot is a private chat with that user.
        self.chat_id = sender.id
        self.is_private = True
        self.client = client
        self.data = data.encode() if isinstance(data, str) else data
        self.message = FakeMessage(text)
        self.photo = None
        self.document = None
        self.media_bytes = self.PNG_BYTES
        self.replies = []
        self.keyboards = []
        self.answered = None
        self.auto_answered = False
        self.lost_toasts = []

    async def get_sender(self):
        return self.sender

    async def answer(self, text=None, alert=False):
        # Telethon's answer() is idempotent. An edit/respond may consume the answer
        # before a later toast is issued.
        if self.auto_answered:
            if text:
                self.lost_toasts.append(text)
            return
        self.auto_answered = True
        if self.answered is None:
            self.answered = text

    async def respond(self, text=None, buttons=None, parse_mode=None, file=None):
        self._auto_answer()
        if file is not None and text and len(text) > 1024:
            failures.append(f"caption over the 1024 limit: {len(text)} chars")
        self.replies.append(text)
        self.keyboards.append(buttons)
        return object()

    async def edit(self, text=None, buttons=None, parse_mode=None):
        self._auto_answer()
        self.replies.append(text)
        self.keyboards.append(buttons)
        return object()

    async def delete(self):
        self._auto_answer()
        return None

    def _auto_answer(self):
        # edit/respond/reply/delete each schedule answer() before their own send.
        if self.data is not None:
            self.auto_answered = True

    # Real PNG bytes: the bot checks the signature of what it downloaded, because the
    # mime type is whatever the sending client claimed it was.
    PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"receipt"

    async def download_media(self, path):
        # Telethon appends the extension it guessed when the given path has none
        # (client/downloads.py _get_proper_filename). Modelling that is the whole
        # point: without it the harness certified a handler that always failed.
        import os

        directory, name = os.path.split(path)
        stem, ext = os.path.splitext(name)
        if not ext:
            ext = ".pdf" if self.media_bytes.startswith(b"%PDF") else ".jpg"
        path = os.path.join(directory, stem + ext)
        with open(path, "wb") as handle:
            handle.write(self.media_bytes)
        return path


client = TelegramClient(MemorySession(), config.API_ID, config.API_HASH)
register_all(client)
HANDLERS = client.list_event_handlers()
CALLBACK_HANDLERS = [(cb, b) for cb, b in HANDLERS if type(b).__name__ == "CallbackQuery"]
MESSAGE_HANDLERS = [(cb, b) for cb, b in HANDLERS if type(b).__name__ == "NewMessage"]
# Stars payments enter through the registered Raw handler.
RAW_HANDLERS = [(cb, b) for cb, b in HANDLERS if type(b).__name__ == "Raw"]

fake = FakeClient()

# Handlers registered with @client.on close over this client object, and the Raw payment
# handler passes it on to stars.refund and notify.to_user. Redirecting its two send
# methods is what makes that outgoing traffic visible to the harness.
client.send_message = fake.send_message
client.send_file = fake.send_file
# __call__ is looked up on the class, so an instance attribute would never be used.
type(client).__call__ = lambda self, request, *args, **kwargs: fake(request)


def produced(event) -> str:
    return "\n".join(text for text in event.replies if text) or (event.answered or "")


def record(label, event, expect):
    text = produced(event)
    if expect and expect.lower() not in text.lower():
        failures.append(f"{label}: expected '{expect}', got '{text[:100]}'")
    for lost in getattr(event, "lost_toasts", []):
        failures.append(f"{label}: the toast '{lost}' was rendered away and never shown")
    first_line = text.strip().splitlines()[0] if text.strip() else "(no reply)"
    steps.append(f"{label:<34} {first_line[:70]}")


def clear_state(sender) -> None:
    states.clear(sender.id, sender.id)


async def click(sender, data, expect=None, label=None, event_client=None):
    label = label or data
    event = FakeEvent(sender, event_client or fake, data=data)
    matched = 0
    for callback, builder in CALLBACK_HANDLERS:
        if builder.match and builder.match(event.data):
            matched += 1
            try:
                await callback(event)
            except Exception as error:
                failures.append(f"{label}: {type(error).__name__}: {error}")
                traceback.print_exc()
    if not matched:
        failures.append(f"{label}: no handler answers this button")
    record(label, event, expect)
    return event


async def send_text(sender, text, expect=None, label=None):
    label = label or f"text: {text[:24]}"
    event = FakeEvent(sender, fake, text=text)
    for callback, builder in MESSAGE_HANDLERS:
        pattern_ok = builder.pattern is None or builder.pattern(text)
        func_ok = True
        if builder.func:
            try:
                func_ok = builder.func(event)
            except Exception:
                func_ok = False
        if pattern_ok and func_ok:
            try:
                await callback(event)
            except Exception as error:
                failures.append(f"{label}: {type(error).__name__}: {error}")
                traceback.print_exc()
    record(label, event, expect)
    return event


async def deliver_raw(update, label=None):
    # Feeds a real Telegram update object through the registered Raw handlers, the way
    # Telethon's dispatcher does.
    for callback, builder in RAW_HANDLERS:
        if builder.filter(update) is None:
            continue
        try:
            await callback(update)
        except Exception as error:
            failures.append(f"{label or 'raw update'}: {type(error).__name__}: {error}")
            traceback.print_exc()


async def start_stars_invoice(sender, product_id, label="pay with stars"):
    await click(sender, f"pay_stars:{product_id}", expect="confirm your purchase", label=label)
    quote = await connection.fetch_one("SELECT token FROM invoices ORDER BY id DESC")
    return await click(
        sender,
        f"pay_stars_confirm:{quote['token']}",
        expect="invoice sent",
        label=f"{label} confirmation",
    )


async def pay_last_invoice(
    charge_id="charge",
    amount=None,
    currency="XTR",
    sender_id=None,
    provider_charge_id=None,
):
    # Replays what Telegram sends after a successful Stars payment, including the amount
    # it actually charged: the bot compares it with the price before granting anything.
    invoice = await connection.fetch_one(
        "SELECT token, product_id, amount_stars FROM invoices ORDER BY id DESC"
    )
    token = invoice["token"]
    product_id = invoice["product_id"]
    if amount is None:
        amount = invoice["amount_stars"]
    provider_charge_id = provider_charge_id or f"provider-{charge_id}"

    action = types.MessageActionPaymentSentMe(
        currency=currency,
        total_amount=amount,
        payload=f"buy:{product_id}:{token}".encode(),
        charge=types.PaymentCharge(id=charge_id, provider_charge_id=provider_charge_id),
    )
    message = types.MessageService(
        id=1,
        peer_id=types.PeerUser(user_id=sender_id or CUSTOMER_ID),
        date=None,
        action=action,
    )
    await deliver_raw(
        types.UpdateNewMessage(message=message, pts=0, pts_count=1), label="stars payment"
    )


async def customer_journey(customer):
    await send_text(customer, "/start", expect="pick a section")
    await send_text(customer, "/terms", expect="payment terms")
    await send_text(customer, "/support", expect="payment support")
    await send_text(customer, "/paysupport", expect="payment support")
    await click(customer, "menu:catalog", expect="catalog")
    await click(customer, "prod:1", expect="Music Premium")
    await start_stars_invoice(customer, 1)
    await pay_last_invoice("charge-1")

    customer_row = await users_db.get(CUSTOMER_ID)
    if not await subs_db.active_for_slug(customer_row["id"], "music"):
        failures.append("stars payment: no subscription was created")
    if (await orders_db.get(1))["status"] != orders_db.PAID:
        failures.append("stars payment: order did not become paid")
    first_order = await orders_db.get(1)
    if (
        first_order["payment_charge_id"] != "charge-1"
        or first_order["payment_provider_charge_id"] != "provider-charge-1"
    ):
        failures.append("stars payment: the two Telegram charge ids were not stored separately")


async def staff_journey(owner, customer):
    await send_text(owner, "/admin", expect="admin")
    await click(owner, "a:orders", expect="orders in progress")
    await click(owner, "a:order:1", expect="order #1")
    await click(owner, "a:send:1", expect="access details", label="ask for access details")
    await send_text(owner, "login: ann@mail / pass: 12345", expect="saved")
    if (await orders_db.get(1))["status"] != orders_db.DELIVERED:
        failures.append("access details: order did not become delivered")

    await click(customer, "ok:1", expect="thanks")
    await click(customer, "menu:subs", expect="active subscriptions")
    await click(customer, "subdata:1", expect="12345")
    await click(customer, "menu:orders", expect="my orders")
    await click(customer, "menu:help", expect="help")


async def renewal_keeps_the_date(customer):
    subscription = await subs_db.active_for_slug(
        (await users_db.get(CUSTOMER_ID))["id"], "music"
    )
    before = subscription["expires_at"]

    await click(customer, "prod:1", expect="renewal", label="card shows the renewal date")
    await start_stars_invoice(customer, 1, label="renew with stars")
    await pay_last_invoice("charge-2")

    after = (await subs_db.get(subscription["id"]))["expires_at"]
    if (dates.parse(after) - dates.parse(before)).days != 30:
        failures.append(f"renewal: expected 30 more days, got {before} -> {after}")


async def transfer_journey(owner, customer):
    method_id = await requisites_db.create("Card", "card", "1111222233334444", "Bank", "Boss")
    await users_db.set_payment_method((await users_db.get(CUSTOMER_ID))["id"], method_id)
    await orders_db.set_status(1, orders_db.COMPLETED)
    await orders_db.set_status(2, orders_db.COMPLETED)

    await click(customer, "prod:2", expect="Cloud Storage")
    await click(customer, "pay_tr:2", expect="bank transfer", label="pay by transfer")

    receipt = FakeEvent(customer, fake)
    receipt.photo = True
    await checkout.handle_receipt(receipt)
    record("send the receipt", receipt, "receipt received")
    if (await orders_db.get(3))["status"] != orders_db.PENDING_REVIEW:
        failures.append("receipt: order did not go to review")

    await click(owner, "a:confirm:3", expect="order #3", label="confirm the transfer")
    if (await orders_db.get(3))["status"] != orders_db.PAID:
        failures.append("transfer: order did not become paid")


async def admin_sections(owner):
    await click(owner, "a:subs", expect="active subscriptions")
    await click(owner, "a:sub:1", expect="subscription #1")
    await click(owner, "a:subdays:1", expect="how many days", label="ask for extra days")
    await send_text(owner, "5", expect="days added")
    await click(owner, "a:subcreds:1", expect="new access details")
    await send_text(owner, "new-login / new-pass", expect="updated")

    await click(owner, "a:users", expect="users")
    await click(owner, "a:staff", expect="staff")
    await click(owner, "a:user:2", expect="ann")
    await click(owner, "a:roles:2", expect="role for")
    await click(owner, "a:role:2:manager", expect="manager")
    await click(owner, "a:role:2:user", label="role back to user")
    await click(owner, "a:block:2:1", label="block")
    await click(owner, "a:block:2:0", label="unblock")
    await click(owner, "a:pm:2", expect="payment details")
    await click(owner, "a:pmset:2:1", label="assign payment details")
    await click(owner, "a:write:222", expect="message for client")
    await send_text(owner, "hello there", expect="sent")

    await click(owner, "a:cat", expect="catalog")
    await click(owner, "a:p:1", expect="music")
    await click(owner, "a:pe:1:price_stars", expect="price in stars")
    await send_text(owner, "250", expect="saved")
    if (await products_db.get(1))["price_stars"] != 250:
        failures.append("price edit: the new price was not stored")
    await click(owner, "a:pe:1:duration_days", expect="length in days")
    await send_text(owner, "0", expect="between 1 and", label="reject a zero period")
    await send_text(owner, "31", expect="saved")
    await click(owner, "a:ptoggle:1:0", label="disable the product")
    await click(owner, "a:ptoggle:1:1", label="enable the product")

    await click(owner, "a:cnew", expect="name of the new product")
    await send_text(owner, "Extra plan", expect="created")
    new_id = (await products_db.get_by_slug("extra-plan"))["id"]
    await click(owner, f"a:pdel:{new_id}", expect="delete")
    await click(owner, f"a:pdelyes:{new_id}", expect="catalog")
    if await products_db.get(new_id):
        failures.append("product delete: the product is still there")

    await click(owner, "a:offers", expect="personal offers")
    await click(owner, "a:offind", expect="who is the offer for")
    await send_text(owner, "ann", expect="offers for")
    await click(owner, "a:uoffers:2", expect="offers for")
    await click(owner, "a:oclone:2", expect="pick a product")
    await click(
        owner, "a:ocl:2:3", expect="personal offer", label="copy an offer to the client"
    )
    personal = await products_db.list_personal(2)
    if len(personal) != 1:
        failures.append("personal offer: it was not created")
    else:
        await click(owner, f"a:pe:{personal[0]['id']}:price_rub", expect="price in rubles")
        await send_text(owner, "99", expect="saved", label="set a personal price")

    await click(owner, "a:req", expect="payment details")
    await click(owner, "a:r:1", expect="card")
    await click(owner, "a:rkind:1", label="switch card and fast payments")
    await click(owner, "a:rtoggle:1", label="disable the details")
    await click(owner, "a:rtoggle:1", label="enable the details")
    await click(owner, "a:rdef:1", label="make them default")
    await click(owner, "a:rnew", expect="internal title")
    await send_text(owner, "Second", expect="phone number", label="wizard: title")
    await send_text(owner, "+70000000001", expect="bank", label="wizard: details")
    await send_text(owner, "Bank2", expect="recipient", label="wizard: bank")
    await send_text(owner, "Owner2", expect="added", label="wizard: recipient")
    await click(owner, "a:rdel:2", expect="delete")
    await click(owner, "a:rdelyes:2", expect="deleted")

    await click(owner, "a:set", expect="settings")
    await click(owner, "a:sete:bot_name", expect="bot name")
    await send_text(owner, "My Shop", expect="updated")
    if settings.get("bot_name") != "My Shop":
        failures.append("settings: the new bot name was not stored")
    await click(owner, "a:stats", expect="statistics")
    await click(owner, "a:cast", expect="broadcast text")
    preview = await send_text(owner, "Hello everyone", expect="preview")
    confirm_data = preview.keyboards[-1][0][0].data.decode()
    await click(owner, confirm_data, expect="broadcast finished", label="send the broadcast")
    await click(owner, "a:home", expect="admin")


async def customer_cannot_reach_the_panel(customer):
    for data in ("a:home", "a:cat", "a:users", "a:set"):
        event = await click(customer, data, label=f"customer opens {data}")
        if event.answered != "Not enough rights":
            failures.append(f"access: customer got '{event.answered}' for {data}")


async def run():
    await connection.connect()
    await settings.load()
    await users_db.ensure_owners([OWNER_ID])
    await settings.set_value("manager_username", "manager")
    for item in DEMO_PRODUCTS:
        await products_db.create(owner_user_id=products_db.PUBLIC, is_active=1, **item)

    owner = Sender(OWNER_ID, "boss", "Boss")
    customer = Sender(CUSTOMER_ID, "ann", "Ann")

    await customer_journey(customer)
    await staff_journey(owner, customer)
    await renewal_keeps_the_date(customer)
    await transfer_journey(owner, customer)
    await admin_sections(owner)
    await customer_cannot_reach_the_panel(customer)

    await connection.disconnect()


def main():
    try:
        asyncio.run(_sandbox.run_and_close(run))
    finally:
        _sandbox.cleanup()

    print("\n".join(steps))
    print(f"\n{len(steps)} steps, {len(failures)} failures")
    for item in failures:
        print("  FAIL", item)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
