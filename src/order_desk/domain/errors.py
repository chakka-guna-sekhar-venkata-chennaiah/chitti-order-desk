"""Errors that the tools turn into clear messages for the agent.

Each error has a short `code` so the agent (and the evals) can react to the
kind of problem without parsing the text.
"""


class OrderDeskError(Exception):
    code = "error"

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": str(self)}


class NotFound(OrderDeskError):
    code = "not_found"


class NotAllowed(OrderDeskError):
    """The caller has no permission (wrong dealer, not owner, blocked)."""
    code = "not_allowed"


class IllegalTransition(OrderDeskError):
    code = "illegal_transition"


class InvalidInput(OrderDeskError):
    code = "invalid_input"


class NeedsDealerAnswer(OrderDeskError):
    """The order cannot be confirmed: some lines still need an answer."""
    code = "needs_dealer_answer"


class NotEnoughStock(OrderDeskError):
    code = "not_enough_stock"


class Conflict(OrderDeskError):
    """Somebody else changed the record first (optimistic check failed)."""
    code = "conflict"


class ExternalServiceError(OrderDeskError):
    """The model API or Telegram failed. The call is safe to retry."""
    code = "external_service_error"
