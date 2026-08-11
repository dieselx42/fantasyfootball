"""Platform adapter registry.

Register a new platform by importing its class and adding it to ``ADAPTERS``.
The setup wizard builds its platform list from this registry, so a new adapter
shows up in the UI with no UI changes.
"""

from __future__ import annotations

from typing import Any, Mapping

from .base import (
    CAPABILITIES,
    CAPABILITY_LABELS,
    PlatformAdapter,
    PlatformError,
    PlatformUnsupported,
)
from .espn import EspnAdapter
from .manual import ManualAdapter
from .sleeper import SleeperAdapter
from .yahoo import YahooAdapter

ADAPTERS: dict[str, type[PlatformAdapter]] = {
    cls.kind: cls
    for cls in (YahooAdapter, SleeperAdapter, EspnAdapter, ManualAdapter)
}


def get_adapter(kind: str, settings: Mapping[str, Any] | None = None) -> PlatformAdapter:
    cls = ADAPTERS.get(kind)
    if cls is None:
        raise PlatformError(f"Unknown platform '{kind}'.")
    return cls(settings)


def adapter_for(cfg: Mapping[str, Any]) -> PlatformAdapter:
    platform = cfg.get("platform") or {}
    return get_adapter(platform.get("kind", "manual"), platform.get("settings"))


def describe_all() -> list[dict[str, Any]]:
    """Everything the setup wizard needs to render the platform picker."""
    return [
        {
            "kind": cls.kind,
            "label": cls.label,
            "description": cls.description,
            "requires_auth": cls.requires_auth,
            "fields": [
                {"key": key, "label": label, "help": help_text}
                for key, label, help_text in cls.setup_fields
            ],
            "capabilities": sorted(cls().capabilities()),
        }
        for cls in ADAPTERS.values()
    ]


__all__ = [
    "ADAPTERS",
    "CAPABILITIES",
    "CAPABILITY_LABELS",
    "PlatformAdapter",
    "PlatformError",
    "PlatformUnsupported",
    "adapter_for",
    "describe_all",
    "get_adapter",
]
