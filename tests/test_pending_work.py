"""The owner finds the pending work in the database, not only in Telegram cards.

Live run 2026-10-27: the "new dealer" card was sent before the owner had pressed START in the shop bot.
Telegram answered 400 "chat not found", the outbox gave up at once, and the owner never heard of the
request (KI-066). The shop agent then asked for the dealer's Telegram ID, because no tool showed it
(KI-067). `get_report("pending_work")` reads both from the tables.
"""

from __future__ import annotations

import pytest

from conftest import OWNER, PACKER, RAMESH, line, outbox_rows, reading
from order_desk import clock
from order_desk.agent.loop import ToolAgent, load_instructions
from order_desk.domain.errors import InvalidInput, NotAllowed
from order_desk.mcp_server.tools import shop_tools
from test_agent_loop import ScriptedModel

NEW_DEALER = 900000077
REQUEST = dict(name="Ravi", shop_name="Ravi Electricals", phone="9000000000", city="Vijayawada")


def _lose_the_owner_cards(deps) -> int:
    """What the live run showed: every queued card to the owner ends as `failed` ("chat not found")."""
    with deps.db.write() as conn:
        return conn.execute("UPDATE outbox SET status = 'failed', last_error = 'sendMessage: chat not found' "
                            "WHERE bot = 'shop'").rowcount


def _order_waiting_for_the_owner(dealer, deps, photo) -> int:
    deps.reader.chits.append(reading(line("6A switch - 20 nos (Anchor)", "switch", {"rating_amp": 6},
                                          "Anchor", 20, "nos")))
    order_id = dealer.start_order(RAMESH, "photo", "msg-1")["order_id"]
    dealer.read_chit(RAMESH, order_id, photo())
    dealer.confirm(RAMESH, order_id)
    return order_id


def test_a_lost_new_dealer_card_does_not_hide_the_request_from_the_owner(dealer, shop, deps):
    # Rule: the database is the truth, not the outbox (KI-066).
    dealer.register(NEW_DEALER, **REQUEST)
    assert any(r["dedupe_key"].startswith("dealer_request:") for r in outbox_rows(deps))
    assert _lose_the_owner_cards(deps) >= 1                    # the card never reached the owner

    result = shop.report(OWNER, "pending_work")

    assert result["ok"] is True and result["report"] == "pending_work"
    assert result["data"]["pending_dealers"] == [{
        "dealer_telegram_user_id": NEW_DEALER, "name": "Ravi", "shop": "Ravi Electricals",
        "phone": "9000000000", "city": "Vijayawada", "requested_at": clock.now_iso()}]
    assert result["data"]["orders_waiting_for_approval"] == []


def test_the_owner_can_approve_a_dealer_with_the_id_that_the_report_shows(dealer, shop, deps):
    # Rule: set_dealer_status needs the Telegram ID, so the report must give it (KI-067).
    dealer.register(NEW_DEALER, **REQUEST)
    dealer.register(NEW_DEALER + 1, name="Sai", shop_name="Sai Traders", phone="9111111111", city="Guntur")

    pending = shop.report(OWNER, "pending_work")["data"]["pending_dealers"]
    ravi = next(d for d in pending if d["name"] == "Ravi")
    assert shop.set_dealer_status(OWNER, ravi["dealer_telegram_user_id"], "active") == {
        "ok": True, "dealer": "Ravi", "status": "active"}

    left = shop.report(OWNER, "pending_work")["data"]["pending_dealers"]
    assert [d["name"] for d in left] == ["Sai"]                # only the dealer who is still undecided


def test_pending_work_lists_the_orders_that_wait_for_the_owner(dealer, shop, deps, photo):
    deps.reader.chits.append(reading(line("6A switch - 20 nos (Anchor)", "switch", {"rating_amp": 6},
                                          "Anchor", 20, "nos")))
    order_id = dealer.start_order(RAMESH, "photo", "msg-1")["order_id"]
    dealer.read_chit(RAMESH, order_id, photo())                # read, but the dealer has not said yes yet
    assert shop.report(OWNER, "pending_work")["data"]["orders_waiting_for_approval"] == []

    dealer.confirm(RAMESH, order_id)
    waiting = shop.report(OWNER, "pending_work")["data"]["orders_waiting_for_approval"]
    assert [(w["order_id"], w["dealer"], w["shop"], w["line_count"]) for w in waiting] == [
        (order_id, "Ramesh Babu", "Ramesh Electrical Works", 1)]

    shop.approve(OWNER, order_id)
    assert shop.report(OWNER, "pending_work")["data"]["orders_waiting_for_approval"] == []


def test_a_packer_sees_the_orders_but_not_the_dealer_details(dealer, shop, deps, photo):
    # Phone, city and Telegram ID of a dealer are for the owner, who alone may call set_dealer_status.
    dealer.register(NEW_DEALER, **REQUEST)
    order_id = _order_waiting_for_the_owner(dealer, deps, photo)

    data = shop.report(PACKER, "pending_work")["data"]

    assert "pending_dealers" not in data
    assert [w["order_id"] for w in data["orders_waiting_for_approval"]] == [order_id]


def test_pending_work_is_for_shop_staff_only(shop):
    with pytest.raises(NotAllowed):
        shop.report(RAMESH, "pending_work")                    # a dealer, not staff


def test_the_unknown_report_message_names_pending_work(shop):
    with pytest.raises(InvalidInput, match="pending_work"):
        shop.report(OWNER, "no_such_report")


def test_the_shop_agent_reads_pending_work_for_the_owner_who_wrote_start(dealer, shop, deps):
    # Through the tool set the model really gets: the loop fills the staff ID, the model sends only the name.
    dealer.register(NEW_DEALER, **REQUEST)
    model = ScriptedModel([[("get_report", {"name": "pending_work"})], "Hello. One dealer request waits."])
    agent = ToolAgent(shop_tools(shop, dealer), load_instructions("shop"), model, "fake")

    reply, _, trace = agent.run([], "[telegram message_id=1 from_user_id=111]\n/start", caller_id=OWNER)

    result = trace.tool_calls()[0]["result"]
    assert result["ok"] is True
    assert result["data"]["pending_dealers"][0]["dealer_telegram_user_id"] == NEW_DEALER
    assert reply == "Hello. One dealer request waits."


def test_shop_instructions_greet_with_the_summary_and_never_ask_for_a_telegram_id():
    text = " ".join(load_instructions("shop").split())
    assert "When the message is `/start`" in text
    assert 'Call `get_report` with `name="pending_work"`' in text
    assert "Never ask the owner for a Telegram ID" in text
    assert "`dealer_telegram_user_id`" in text
