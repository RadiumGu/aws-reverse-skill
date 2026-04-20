"""scanner_interface — Abstract interface for resource scanners.

Decouples the pipeline (select.py, preview.py, rewrite_cfn.py) from the
former2 raw.json schema. To swap scanners (e.g. AWS Config, Steampipe),
implement ``Scanner`` and register it in ``get_scanner()``.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class Resource:
    """Normalized resource representation used across the pipeline.

    Attributes:
        type: AWS resource type (e.g. ``AWS::Lambda::Function``).
        physical_id: Physical resource identifier.
        region: AWS region where the resource lives.
        tags: Key-value tags.
        properties: Raw provider-specific properties.
    """

    __slots__ = ("type", "physical_id", "region", "tags", "properties")

    def __init__(
        self,
        type: str,
        physical_id: str = "",
        region: str = "",
        tags: dict[str, str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        self.type = type
        self.physical_id = physical_id
        self.region = region
        self.tags = tags or {}
        self.properties = properties or {}

    def to_dict(self) -> dict[str, Any]:
        """Convert to the dict format expected by select.py / preview.py."""
        d: dict[str, Any] = dict(self.properties)
        d["Type"] = self.type
        if self.physical_id:
            d.setdefault("PhysicalResourceId", self.physical_id)
        if self.region:
            d.setdefault("Region", self.region)
        if self.tags:
            d.setdefault("Tags", self.tags)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Resource:
        """Create from a former2 raw.json resource dict."""
        return cls(
            type=data.get("Type", ""),
            physical_id=data.get("PhysicalResourceId", ""),
            region=data.get("Region", ""),
            tags=data.get("Tags") if isinstance(data.get("Tags"), dict) else {},
            properties={
                k: v
                for k, v in data.items()
                if k not in ("Type", "PhysicalResourceId", "Region", "Tags")
            },
        )


class Scanner(ABC):
    """Abstract base class for resource scanners."""

    @abstractmethod
    def scan(
        self,
        region: str,
        services: list[str] | None = None,
        profile: str | None = None,
    ) -> list[Resource]:
        """Scan AWS resources and return normalized Resource objects."""
        ...

    @abstractmethod
    def name(self) -> str:
        """Human-readable scanner name."""
        ...


class Former2Scanner(Scanner):
    """Scanner backed by an existing former2 raw.json file.

    This is the default — it reads an already-produced raw.json rather than
    invoking former2 directly. The scan.js Node script handles the actual
    former2 invocation.
    """

    def __init__(self, raw_path: Path) -> None:
        self._path = raw_path

    def name(self) -> str:
        return "former2"

    def scan(
        self,
        region: str = "",
        services: list[str] | None = None,
        profile: str | None = None,
    ) -> list[Resource]:
        data = json.loads(self._path.read_text(encoding="utf-8"))
        items: list[dict[str, Any]]
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("resources", [])
        else:
            raise ValueError(f"Unexpected JSON structure in {self._path}")
        return [Resource.from_dict(item) for item in items if isinstance(item, dict)]


def load_resources(path: Path) -> list[Resource]:
    """Convenience: load resources from a raw.json file via Former2Scanner."""
    return Former2Scanner(path).scan()


def load_resources_as_dicts(path: Path) -> list[dict[str, Any]]:
    """Load resources as plain dicts (backward-compatible with existing code)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("resources", [])
    raise ValueError(f"Unexpected JSON structure in {path}")
