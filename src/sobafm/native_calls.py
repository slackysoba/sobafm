"""Optional per-owner guard at the explicit native function boundary."""

from collections.abc import Callable
from typing import Any


class BudgetExhaustedError(RuntimeError):
    pass


class GuardedAPI:
    """Charge before forwarding, including calls made by fallback/close helpers."""

    def __init__(self, api: Any, charge: Callable[[], None]) -> None:
        self.api, self.charge = api, charge

    def __getattr__(self, name: str) -> Any:
        function = getattr(self.api, name)

        def call(*args: Any) -> Any:
            self.charge()
            return function(*args)

        return call
