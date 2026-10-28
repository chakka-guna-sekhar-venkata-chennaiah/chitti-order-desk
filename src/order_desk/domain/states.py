"""The order state machine.

An order is in exactly one state. Only the moves in TRANSITIONS are legal,
and each move names the kind of actor that may cause it. The services call
`check_transition()` before every state change, so a model (or a bug) cannot
skip a step such as owner approval.

    RECEIVED -> PARSED -> AWAITING_DEALER -> CONFIRMED -> AWAITING_OWNER
             -> APPROVED -> PACKING -> PACKED -> DISPATCHED

    Exits: REJECTED (owner), EXPIRED (timer), CANCELLED (dealer or duplicate)

See docs/04-order-states.md for a diagram and the reason for each state.
"""

from __future__ import annotations

from enum import Enum

from order_desk.domain.errors import IllegalTransition


class State(str, Enum):
    RECEIVED = "RECEIVED"              # photo/text arrived; nothing read yet
    PARSED = "PARSED"                  # lines read and matched (short, internal step)
    AWAITING_DEALER = "AWAITING_DEALER"  # read-back sent; the dealer must check it
    CONFIRMED = "CONFIRMED"            # dealer said yes; stock reserved
    AWAITING_OWNER = "AWAITING_OWNER"  # approval card sent to the owner
    APPROVED = "APPROVED"              # owner said yes; stock removed; bill made
    PACKING = "PACKING"                # packing slip sent to the team
    PACKED = "PACKED"                  # team finished; dealer told
    DISPATCHED = "DISPATCHED"          # left the shop (demo end, optional)
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class Actor(str, Enum):
    SYSTEM = "system"   # code: tools and the scheduler
    DEALER = "dealer"
    OWNER = "owner"
    PACKER = "packer"


S, A = State, Actor

# (from, to) -> actors that may make this move
TRANSITIONS: dict[tuple[State, State], frozenset[Actor]] = {
    (S.RECEIVED, S.PARSED): frozenset({A.SYSTEM}),
    (S.PARSED, S.AWAITING_DEALER): frozenset({A.SYSTEM}),
    # the dealer edits lines: the order stays in AWAITING_DEALER (a self-move)
    (S.AWAITING_DEALER, S.AWAITING_DEALER): frozenset({A.DEALER, A.SYSTEM}),
    (S.AWAITING_DEALER, S.CONFIRMED): frozenset({A.DEALER}),
    (S.CONFIRMED, S.AWAITING_OWNER): frozenset({A.SYSTEM}),
    # the dealer wants a change before the owner decided: back to the check step
    (S.AWAITING_OWNER, S.AWAITING_DEALER): frozenset({A.DEALER}),
    (S.AWAITING_OWNER, S.APPROVED): frozenset({A.OWNER}),
    (S.AWAITING_OWNER, S.REJECTED): frozenset({A.OWNER}),
    (S.APPROVED, S.PACKING): frozenset({A.SYSTEM}),
    (S.PACKING, S.PACKED): frozenset({A.PACKER, A.OWNER}),
    (S.PACKED, S.DISPATCHED): frozenset({A.OWNER, A.PACKER}),
    (S.AWAITING_DEALER, S.EXPIRED): frozenset({A.SYSTEM}),
    # the dealer may cancel at any time before the owner decides
    (S.RECEIVED, S.CANCELLED): frozenset({A.DEALER, A.SYSTEM}),
    (S.PARSED, S.CANCELLED): frozenset({A.DEALER, A.SYSTEM}),
    (S.AWAITING_DEALER, S.CANCELLED): frozenset({A.DEALER, A.SYSTEM}),
    (S.AWAITING_OWNER, S.CANCELLED): frozenset({A.DEALER}),
}

# States where the order is still "open" for the dealer.
OPEN_FOR_DEALER = frozenset({S.RECEIVED, S.PARSED, S.AWAITING_DEALER})
# States where stock is held by an active reservation.
HOLDS_RESERVATION = frozenset({S.CONFIRMED, S.AWAITING_OWNER})
FINAL = frozenset({S.DISPATCHED, S.REJECTED, S.EXPIRED, S.CANCELLED})


def check_transition(current: State | str, target: State | str, actor: Actor) -> None:
    """Raise IllegalTransition if `actor` may not move the order to `target`."""
    current, target = State(current), State(target)
    allowed = TRANSITIONS.get((current, target))
    if allowed is None:
        raise IllegalTransition(f"An order in {current.value} cannot move to {target.value}.")
    if actor not in allowed:
        who = ", ".join(sorted(a.value for a in allowed))
        raise IllegalTransition(
            f"{actor.value} cannot move an order from {current.value} to {target.value} "
            f"(allowed: {who})."
        )
