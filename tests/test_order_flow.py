"""End-to-end order flow on the service layer: chitti -> read-back -> confirm ->
approval -> packing -> packed. Each test checks one rule from the design."""

from __future__ import annotations

import pytest

from conftest import OWNER, PACKER, RAMESH, SURESH, VENKAT, line, outbox_rows, reading, stock_of
from order_desk.domain.errors import IllegalTransition, NotAllowed, NotFound


def _order_with(dealer, deps, photo, *lines, who=RAMESH, ref="msg-1"):
    deps.reader.chits.append(reading(*lines))
    order_id = dealer.start_order(who, "photo", ref)["order_id"]
    return order_id, dealer.read_chit(who, order_id, photo())


def test_happy_path_moves_stock_only_after_owner_approval(dealer, shop, deps, photo):
    before = stock_of(deps, "SW-6A-ANCHOR")
    order_id, result = _order_with(
        dealer, deps, photo,
        line("6A switch - 20 nos (Anchor)", "switch", {"rating_amp": 6}, "Anchor", 20, "nos"),
        line("1.5 sq mm wire red - 2 coil (Finolex)", "wire", {"size_sqmm": 1.5, "color": "red"}, "Finolex", 2, "coil"),
    )
    assert result["state"] == "AWAITING_DEALER"
    assert result["needs_answers"] == []
    assert "Reply YES" in result["readback"]

    confirmed = dealer.confirm(RAMESH, order_id)
    assert confirmed["state"] == "AWAITING_OWNER"
    after_confirm = stock_of(deps, "SW-6A-ANCHOR")
    assert after_confirm["on_hand"] == before["on_hand"]          # nothing removed yet
    assert after_confirm["reserved"] == 20                         # only held

    approved = shop.approve(OWNER, order_id)
    assert approved["state"] == "PACKING"
    after_approve = stock_of(deps, "SW-6A-ANCHOR")
    assert after_approve["on_hand"] == before["on_hand"] - 20
    assert after_approve["reserved"] == 0

    packed = shop.mark_packed(PACKER, order_id)
    assert packed["state"] == "PACKED"
    texts = [r["payload_json"] for r in outbox_rows(deps)]
    assert any("NEW ORDER" in t for t in texts)
    assert any("PACKING SLIP" in t for t in texts)
    assert any("is packed" in t for t in texts)


def test_blocked_dealer_cannot_start_an_order(dealer):
    with pytest.raises(NotAllowed):
        dealer.start_order(VENKAT, "photo", "msg-9")


def test_start_order_is_idempotent_for_the_same_message(dealer):
    first = dealer.start_order(RAMESH, "photo", "msg-77")
    second = dealer.start_order(RAMESH, "photo", "msg-77")
    assert first["order_id"] == second["order_id"]
    assert second["created"] is False


def test_read_chit_twice_does_not_call_the_model_twice(dealer, deps, photo):
    order_id, _ = _order_with(dealer, deps, photo,
                              line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos"))
    calls = deps.reader.calls
    again = dealer.read_chit(RAMESH, order_id, photo())
    assert deps.reader.calls == calls
    assert again["state"] == "AWAITING_DEALER"


def test_same_photo_two_times_keeps_one_order(dealer, deps, tmp_path):
    from PIL import Image
    path = tmp_path / "same.jpg"
    Image.new("RGB", (30, 30), (10, 20, 30)).save(path)
    deps.reader.chits.append(reading(line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos")))
    first = dealer.start_order(RAMESH, "photo", "m1")["order_id"]
    dealer.read_chit(RAMESH, first, f"file:{path}")
    second = dealer.start_order(RAMESH, "photo", "m2")["order_id"]
    result = dealer.read_chit(RAMESH, second, f"file:{path}")
    assert result["duplicate_of"] == first
    assert result["state"] == "CANCELLED"


def test_similar_chitti_is_flagged_as_possible_duplicate(dealer, deps, photo):
    lines = [line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos"),
             line("mcb 32 - 2", "mcb", {"rating_amp": 32}, "Havells", 2, "nos")]
    first, _ = _order_with(dealer, deps, photo, *lines, ref="a")
    second, result = _order_with(dealer, deps, photo, *lines, ref="b")
    assert result["possible_duplicate_of"] == first
    assert f"order #{first}" in result["readback"]


def test_unclear_line_blocks_confirmation_until_answered(dealer, deps, photo):
    order_id, result = _order_with(
        dealer, deps, photo,
        line("16A ' - 3", "switch", {"rating_amp": 16}, None, 3, "nos", uses_ditto=True,
             ambiguity="Ditto mark may mean switch or socket."),
    )
    assert result["needs_answers"] == [1]
    blocked = dealer.confirm(RAMESH, order_id)
    assert blocked["ok"] is False and blocked["error"] == "needs_dealer_answer"
    dealer.update_lines(RAMESH, order_id, [{"line_index": 1, "sku": "SOC-16A-ANCHOR", "qty": 3}])
    assert dealer.confirm(RAMESH, order_id)["state"] == "AWAITING_OWNER"


def test_short_stock_is_marked_and_lower_quantity_fixes_it(dealer, deps, photo):
    sellable = stock_of(deps, "FAN-1200-ORIENT")["sellable"]
    order_id, result = _order_with(
        dealer, deps, photo,
        line("Fan 1200mm - 50 nos (Orient)", "ceiling_fan", {"size_mm": 1200}, "Orient", 50, "nos"))
    assert result["lines"][0]["status"] == "short"
    dealer.update_lines(RAMESH, order_id, [{"line_index": 1, "qty": sellable}])
    assert dealer.confirm(RAMESH, order_id)["state"] == "AWAITING_OWNER"


def test_dealer_cannot_see_another_dealers_order(dealer, deps, photo):
    order_id, _ = _order_with(dealer, deps, photo,
                              line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos"))
    with pytest.raises(NotFound):
        dealer.order_status(SURESH, order_id)


def test_packer_cannot_approve(dealer, shop, deps, photo):
    order_id, _ = _order_with(dealer, deps, photo,
                              line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos"))
    dealer.confirm(RAMESH, order_id)
    with pytest.raises(NotAllowed):
        shop.approve(PACKER, order_id)


def test_approval_card_shows_overdue_credit(dealer, deps, photo):
    order_id, _ = _order_with(dealer, deps, photo,
                              line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos"), who=SURESH)
    dealer.confirm(SURESH, order_id)
    card = [r for r in outbox_rows(deps) if "NEW ORDER" in r["payload_json"]][0]
    assert "OVERDUE" in card["payload_json"]


def test_owner_edit_is_reported_to_dealer(dealer, shop, deps, photo):
    order_id, _ = _order_with(dealer, deps, photo,
                              line("6A sw - 20", "switch", {"rating_amp": 6}, "Anchor", 20, "nos"))
    dealer.confirm(RAMESH, order_id)
    result = shop.approve(OWNER, order_id, edits=[{"line_index": 1, "qty": 15}])
    assert result["changes"] == ["line 1: quantity 20 -> 15 nos"]
    assert stock_of(deps, "SW-6A-ANCHOR")["reserved"] == 0
    note = [r for r in outbox_rows(deps) if "is approved" in r["payload_json"]][0]
    assert "20 -> 15" in note["payload_json"]


def test_approve_twice_deducts_once(dealer, shop, deps, photo):
    before = stock_of(deps, "SW-6A-ANCHOR")["on_hand"]
    order_id, _ = _order_with(dealer, deps, photo,
                              line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos"))
    dealer.confirm(RAMESH, order_id)
    shop.approve(OWNER, order_id)
    again = shop.approve(OWNER, order_id)
    assert "already approved" in again["message"]
    assert stock_of(deps, "SW-6A-ANCHOR")["on_hand"] == before - 10


def test_reject_releases_reservation(dealer, shop, deps, photo):
    order_id, _ = _order_with(dealer, deps, photo,
                              line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos"))
    dealer.confirm(RAMESH, order_id)
    assert stock_of(deps, "SW-6A-ANCHOR")["reserved"] == 10
    shop.reject(OWNER, order_id, "Clear the old bill first")
    assert stock_of(deps, "SW-6A-ANCHOR")["reserved"] == 0


def test_reopen_returns_order_to_dealer_and_releases_stock(dealer, deps, photo):
    order_id, _ = _order_with(dealer, deps, photo,
                              line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos"))
    dealer.confirm(RAMESH, order_id)
    result = dealer.reopen(RAMESH, order_id)
    assert result["state"] == "AWAITING_DEALER"
    assert stock_of(deps, "SW-6A-ANCHOR")["reserved"] == 0


def test_confirm_again_after_reopen_holds_the_stock_again(dealer, shop, deps, photo):
    """KI-001: `reservations` is UNIQUE (order_id, sku), so a reopened order must be able to hold
    the SKU it held before. Confirm, reopen, confirm again, then approve."""
    before = stock_of(deps, "SW-6A-ANCHOR")
    order_id, _ = _order_with(dealer, deps, photo,
                              line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos"))
    dealer.confirm(RAMESH, order_id)
    dealer.reopen(RAMESH, order_id)
    assert stock_of(deps, "SW-6A-ANCHOR")["reserved"] == 0

    again = dealer.confirm(RAMESH, order_id)
    assert again["ok"] is True
    assert again["state"] == "AWAITING_OWNER"
    held = stock_of(deps, "SW-6A-ANCHOR")
    assert held["reserved"] == 10                              # held once, not twice
    assert held["on_hand"] == before["on_hand"]                # nothing removed before approval
    cards = [r for r in outbox_rows(deps) if "NEW ORDER" in r["payload_json"]]
    assert len(cards) == 2                                     # the owner is asked again

    approved = shop.approve(OWNER, order_id)
    assert approved["state"] == "PACKING"
    after = stock_of(deps, "SW-6A-ANCHOR")
    assert after["on_hand"] == before["on_hand"] - 10
    assert after["reserved"] == 0


def test_confirm_again_after_reopen_holds_only_the_lines_that_are_left(dealer, shop, deps, photo):
    """After a reopen the dealer may change the order. A SKU held before comes back with its new
    quantity, a removed SKU stays free, a new SKU is held for the first time. A second round works too."""
    switch = stock_of(deps, "SW-6A-ANCHOR")
    order_id, _ = _order_with(
        dealer, deps, photo,
        line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos"),
        line("1.5 sq mm wire red - 2 coil (Finolex)", "wire", {"size_sqmm": 1.5, "color": "red"}, "Finolex", 2, "coil"),
    )
    dealer.confirm(RAMESH, order_id)
    assert stock_of(deps, "WIRE-1.5-RED-FINOLEX")["reserved"] == 2
    dealer.reopen(RAMESH, order_id)
    dealer.update_lines(RAMESH, order_id, [
        {"line_index": 1, "qty": 6},                           # held before, smaller quantity now
        {"line_index": 2, "remove": True},                     # held before, must not come back
        {"add_sku": "EXH-HAVELLS", "qty": 1},                  # never held by this order
    ])

    assert dealer.confirm(RAMESH, order_id)["state"] == "AWAITING_OWNER"
    assert stock_of(deps, "SW-6A-ANCHOR")["reserved"] == 6
    assert stock_of(deps, "WIRE-1.5-RED-FINOLEX")["reserved"] == 0
    assert stock_of(deps, "EXH-HAVELLS")["reserved"] == 1

    dealer.reopen(RAMESH, order_id)                            # a second round
    assert stock_of(deps, "SW-6A-ANCHOR")["reserved"] == 0
    assert dealer.confirm(RAMESH, order_id)["state"] == "AWAITING_OWNER"
    assert stock_of(deps, "SW-6A-ANCHOR")["reserved"] == 6
    assert stock_of(deps, "EXH-HAVELLS")["reserved"] == 1

    assert shop.approve(OWNER, order_id)["state"] == "PACKING"
    assert stock_of(deps, "SW-6A-ANCHOR")["on_hand"] == switch["on_hand"] - 6
    assert stock_of(deps, "SW-6A-ANCHOR")["reserved"] == 0
    assert stock_of(deps, "EXH-HAVELLS")["reserved"] == 0


def test_cannot_pack_before_approval(dealer, shop, deps, photo):
    order_id, _ = _order_with(dealer, deps, photo,
                              line("6A sw - 10", "switch", {"rating_amp": 6}, "Anchor", 10, "nos"))
    with pytest.raises(IllegalTransition):
        shop.mark_packed(PACKER, order_id)


def test_two_dealers_cannot_both_reserve_the_last_units(dealer, deps, photo):
    sellable = stock_of(deps, "EXH-HAVELLS")["sellable"]
    l = line("Exhaust fan Havells", "exhaust_fan", {}, "Havells", sellable, "nos")
    a, _ = _order_with(dealer, deps, photo, l, who=RAMESH, ref="x1")
    b, _ = _order_with(dealer, deps, photo, l, who=SURESH, ref="x2")
    assert dealer.confirm(RAMESH, a)["state"] == "AWAITING_OWNER"
    second = dealer.confirm(SURESH, b)
    assert second["ok"] is False                      # line is now 'short'
    assert second["lines"][0]["status"] == "short"
