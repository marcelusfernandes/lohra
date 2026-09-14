"""Typed, token-free auth refusal shared by subscription and provider routing."""


class SubscriptionError(Exception):
    """Subscription auth is unusable; retry/login requires an operator action."""
