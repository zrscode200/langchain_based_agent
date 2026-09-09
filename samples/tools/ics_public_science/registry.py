"""Runtime transport and disclosure-scoped provider access."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from disclosure import DisclosureTracker
from http_client import ProviderHttpClient


@dataclass(slots=True)
class RuntimeRegistry:
    http: ProviderHttpClient

    @classmethod
    def load(cls) -> "RuntimeRegistry":
        return cls(http=ProviderHttpClient())


class ScopedHttpClient:
    """Permit calls only after every provider-bound field clears preflight."""

    def __init__(
        self,
        base: ProviderHttpClient,
        tracker: DisclosureTracker,
        sent_fields: list[str],
    ) -> None:
        self.base = base
        self.tracker = tracker
        self.sent_fields = sent_fields

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        self.tracker.assert_cleared(self.sent_fields)
        kwargs["before_attempt"] = lambda: self.tracker.record_call(
            self.sent_fields
        )
        return await self.base.get(*args, **kwargs)
