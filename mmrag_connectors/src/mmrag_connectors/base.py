"""Connector plugin contract.

A connector turns an external system into a stream of ``Document`` objects.
It never talks to LightRAG; ingestion is the caller's job (see ``lightrag.py``).

To add a connector: subclass ``Connector``, set ``type``/``label``/``config_schema``,
implement ``fetch``, and register it either with ``@register`` or through the
``mmrag.connectors`` entry-point group in your package's pyproject.toml.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from importlib import metadata, resources
from typing import Any, Callable, ClassVar, Iterator


@dataclass
class Document:
    external_id: str          # stable id within the source, e.g. "confluence:12345"
    title: str
    content: str              # markdown / plain text
    file_source: str          # name shown as the citation in LightRAG; must be unique per document
    url: str = ""
    updated: str = ""         # source-side modification time (ISO) if known
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass
class Field_:
    name: str
    label: str
    type: str = "text"        # text | password | number | bool | select | textarea
    required: bool = False
    default: Any = None
    help: str = ""
    options: list[str] = field(default_factory=list)


class ConnectorError(Exception):
    pass


ProgressFn = Callable[[str], None]


class Connector:
    type: ClassVar[str] = ""
    label: ClassVar[str] = ""
    guide: ClassVar[str] = ""                      # name of a guides/<guide>.md file
    config_schema: ClassVar[list[Field_]] = []

    def __init__(self, config: dict[str, Any], log: ProgressFn | None = None):
        self.config = self.validate(config)
        self.log: ProgressFn = log or (lambda msg: None)

    @classmethod
    def validate(cls, config: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in cls.config_schema:
            v = config.get(f.name, f.default)
            if v in (None, "") and f.required:
                raise ConnectorError(f"'{f.label}' is required")
            if f.type == "number" and v not in (None, ""):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ConnectorError(f"'{f.label}' must be a number")
            if f.type == "bool":
                v = v in (True, "true", "on", "1", 1, "True")
            out[f.name] = v
        return out

    def test(self) -> str:
        """Cheap connectivity/credential check. Return a human message or raise ConnectorError."""
        return "ok"

    def fetch(self, since: datetime | None = None) -> Iterator[Document]:
        """Yield documents. ``since`` is a hint for incremental sync; callers still de-duplicate by hash."""
        raise NotImplementedError

    def close(self) -> None:
        """Release connections. Also called on leaving a ``with`` block."""

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @classmethod
    def guide_text(cls) -> str:
        if not cls.guide:
            return ""
        try:
            return resources.files("mmrag_connectors").joinpath(f"guides/{cls.guide}.md").read_text("utf-8")
        except (FileNotFoundError, OSError):
            return ""

    @classmethod
    def schema_dict(cls) -> list[dict[str, Any]]:
        return [f.__dict__ for f in cls.config_schema]


_REGISTRY: dict[str, type[Connector]] = {}


def register(cls: type[Connector]) -> type[Connector]:
    _REGISTRY[cls.type] = cls
    return cls


def available() -> dict[str, type[Connector]]:
    """Built-ins plus anything in the ``mmrag.connectors`` entry-point group."""
    from . import confluence, jira  # noqa: F401  (registers built-ins)
    for ep in metadata.entry_points(group="mmrag.connectors"):
        try:
            cls = ep.load()
            _REGISTRY.setdefault(cls.type, cls)
        except Exception:  # a broken plugin must not take the portal down
            continue
    return dict(_REGISTRY)


def get(type_: str) -> type[Connector]:
    try:
        return available()[type_]
    except KeyError:
        raise ConnectorError(f"unknown connector type '{type_}'")
