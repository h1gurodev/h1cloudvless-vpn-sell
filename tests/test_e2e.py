"""End-to-end test of the full user journey driving the real handler functions.

Telegram (CallbackQuery/Message/Bot), the H1Cloud node, and the 2328.io
payment client are replaced with in-memory fakes, but the handler code,
the real Database, config parsing, VPNService, and poller all run for real.

Run: python tests/test_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

os.environ.update({
    "BOT_TOKEN": "test",
    "ADMIN_IDS": "999",
    "PLANS": "m1:1 месяц:30:149:0:3;m3:3 месяца:90:399:0:3",
    "TRIAL_DAYS": "3",
    "TRIAL_TRAFFIC_GB": "10",
    "PAY_ENABLED": "true",
    "PAY_PROJECT_UUID": "proj",
    "PAY_API_KEY": "key",
    "PAY_CURRENCY": "RUB",
    "TOPUP_AMOUNTS": "100,500",
    "SUB_PUBLIC_URL": "https://node.example/sub/{uuid}",
})

# Import after env is set so load_settings() picks it up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vpnsell.config import load_settings  # noqa: E402
from vpnsell.db import Database  # noqa: E402
from vpnsell.vpn_service import VPNService  # noqa: E402
from vpnsell.bot import handlers, topup, admin  # noqa: E402
from vpnsell import poller  # noqa: E402


# --- fakes ---

class FakeMessage:
    def __init__(self):
        self.text = None
        self.markup = None
        self.photos = []

    async def edit_text(self, text, reply_markup=None, **kw):
        self.text = text
        self.markup = reply_markup

    async def answer(self, text, reply_markup=None, **kw):
        self.text = text
        self.markup = reply_markup

    async def answer_photo(self, photo, caption=None, **kw):
        self.photos.append(caption)


class FakeUser:
    def __init__(self, uid, username):
        self.id = uid
        self.username = username
        self.first_name = username


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class FakeCallback:
    def __init__(self, uid, data, username="user"):
        self.from_user = FakeUser(uid, username)
        self.data = data
        self.message = FakeMessage()
        self.answers = []
        self.bot = FakeBot()

    async def answer(self, text="", show_alert=False, **kw):
        self.answers.append((text, show_alert))


class FakeNode:
    """Stand-in for H1CloudClient. Tracks created clients; can be forced to fail."""

    def __init__(self):
        self.clients = {}
        self.fail = False
        self._uuid_seq = 0

    async def get_client(self, name):
        return self.clients.get(name)

    async def create_client(self, name, days, *, traffic_limit_gb=0, device_limit=0):
        if self.fail:
            raise RuntimeError("node down")
        self._uuid_seq += 1
        self.clients[name] = {
            "name": name,
            "uuid": f"uuid-{self._uuid_seq}",
            "expires_at": 0,
            "links": {"ws": f"vless://{name}@node?type=ws#H1 WS"},
        }
        return {"ok": True}

    async def renew_client(self, name, add_days):
        if self.fail:
            raise RuntimeError("node down")
        return {"ok": True}

    async def set_limits(self, name, **kw):
        return {"ok": True}

    async def get_links(self, name):
        c = self.clients.get(name)
        return list(c["links"].values()) if c else []


class FakePay:
    def __init__(self):
        self.status = "check"
        self.created = []

    async def create_payment(self, *, amount, currency, order_id, description="", ttl_seconds=3600):
        uuid = f"pay-{len(self.created)+1}"
        self.created.append(order_id)
        return {"uuid": uuid, "url": f"https://go.2328.io/{uuid}"}

    async def payment_info(self, *, uuid="", order_id=""):
        return {"payment_status": self.status, "uuid": uuid}


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


async def main():
    settings = load_settings()
    path = os.path.join(tempfile.mkdtemp(), "e2e.db")
    db = Database(path)
    await db.connect()
    node = FakeNode()
    vpn = VPNService(settings, db, node)
    pay = FakePay()
    UID = 1001

    print("1) /start registers the user")
    msg = FakeMessage()
    msg.from_user = FakeUser(UID, "alice")
    msg.text = "/start"
    await handlers.cmd_start(msg, settings, db)
    u = await db.get_user(UID)
    check(u is not None, "user created on /start")

    print("2) buy without funds is refused, no charge")
    cb = FakeCallback(UID, "pay:m1")
    await handlers.cb_pay(cb, settings, db, vpn)
    u = await db.get_user(UID)
    check(u.balance == 0, "balance still 0 after failed buy")
    check(not await db.list_subscriptions(UID), "no subscription created")

    print("3) top up 500 via 2328 + poller credits once")
    cb = FakeCallback(UID, "topup:500")
    await topup.cb_topup_amount(cb, settings, db, pay)
    check(len(pay.created) == 1, "payment created at provider")
    pay.status = "paid"
    bot = FakeBot()
    await poller.poll_once(settings, db, pay, bot)
    u = await db.get_user(UID)
    check(u.balance == 500, f"balance credited to 500 (got {u.balance})")
    check(len(bot.sent) == 1, "user notified of top-up")
    # second sweep must not double-credit
    await poller.poll_once(settings, db, pay, bot)
    u = await db.get_user(UID)
    check(u.balance == 500, "no double credit on repeat poll")

    print("4) buy m1 (149) succeeds, balance debited, sub provisioned")
    cb = FakeCallback(UID, "pay:m1")
    await handlers.cb_pay(cb, settings, db, vpn)
    u = await db.get_user(UID)
    check(u.balance == 351, f"balance 500-149=351 (got {u.balance})")
    subs = await db.list_subscriptions(UID)
    check(len(subs) == 1, "one subscription exists")
    check(subs[0].uuid is not None, "subscription has node uuid")
    check("vless://" in cb.message.text, "links shown to user")

    print("5) concurrent double-buy can't overdraw (atomic debit)")
    # Fund exactly one m1 (149). Two concurrent buys -> exactly one succeeds,
    # the other is refused; balance can't go negative.
    await db.adjust_balance(UID, -(u.balance))  # zero it out
    await db.adjust_balance(UID, 149)
    subs_before = len(await db.list_subscriptions(UID))
    cb1 = FakeCallback(UID, "pay:m1")
    cb2 = FakeCallback(UID, "pay:m1")
    await asyncio.gather(
        handlers.cb_pay(cb1, settings, db, vpn),
        handlers.cb_pay(cb2, settings, db, vpn),
    )
    u = await db.get_user(UID)
    subs_after = len(await db.list_subscriptions(UID))
    check(u.balance == 0, f"exactly one charge of 149, balance 0 (got {u.balance})")
    check(subs_after == subs_before, f"no duplicate sub row (got +{subs_after - subs_before})")
    # restore a working balance for later steps
    await db.adjust_balance(UID, 351)

    print("6) node failure during buy refunds the user")
    node.fail = True
    cb = FakeCallback(UID, "pay:m1")
    await handlers.cb_pay(cb, settings, db, vpn)
    u = await db.get_user(UID)
    check(u.balance == 351, f"refunded after node failure (got {u.balance})")
    node.fail = False

    print("7) trial: first claim wins, double-tap refused")
    cb1 = FakeCallback(UID, "trial")
    cb2 = FakeCallback(UID, "trial")
    await asyncio.gather(
        handlers.cb_trial(cb1, settings, db, vpn),
        handlers.cb_trial(cb2, settings, db, vpn),
    )
    trial_subs = [s for s in await db.list_subscriptions(UID) if s.is_trial]
    check(len(trial_subs) == 1, f"exactly one trial created (got {len(trial_subs)})")

    print("8) trial cannot be used a second time")
    cb = FakeCallback(UID, "trial")
    await handlers.cb_trial(cb, settings, db, vpn)
    trial_subs = [s for s in await db.list_subscriptions(UID) if s.is_trial]
    check(len(trial_subs) == 1, "still one trial after retry")
    check(any("использован" in a[0] for a in cb.answers), "user told trial already used")

    print("9) trial rollback: node fails -> claim released, retry works")
    UID2 = 2002
    m2 = FakeMessage(); m2.from_user = FakeUser(UID2, "bob"); m2.text = "/start"
    await handlers.cmd_start(m2, settings, db)
    node.fail = True
    cb = FakeCallback(UID2, "trial")
    await handlers.cb_trial(cb, settings, db, vpn)
    u2 = await db.get_user(UID2)
    check(not u2.trial_used, "trial claim released after node failure")
    node.fail = False
    cb = FakeCallback(UID2, "trial")
    await handlers.cb_trial(cb, settings, db, vpn)
    check(len([s for s in await db.list_subscriptions(UID2) if s.is_trial]) == 1,
          "trial succeeds on retry after node recovers")

    print("10) admin stats reflect activity")
    s = await db.stats()
    check(s["users"] == 2, f"2 users (got {s['users']})")
    check(s["revenue"] == 298, f"revenue=298 from two m1 purchases (got {s['revenue']})")

    await db.close()
    print("\nALL E2E CHECKS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(main())
