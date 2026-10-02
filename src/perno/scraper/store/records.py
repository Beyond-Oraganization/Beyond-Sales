"""Dane przekazywane przez crawler do magazynu."""

from __future__ import annotations

import builtins
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LinkRecord:
    url: str
    url_hash: str
    depth: int
    priority: int
    source: str = "link"


@dataclass
class PageRecord:
    url: str
    url_hash: str
    depth: int
    priority: int
    source: str
    status: str
    parent_id: Optional[int] = None
    http_status: Optional[int] = None
    content_type: Optional[str] = None
    encoding: Optional[str] = None
    bytes: int = 0
    elapsed_ms: Optional[int] = None
    title: Optional[str] = None
    description: Optional[str] = None
    content_hash: Optional[str] = None
    error: Optional[str] = None
    text: Optional[str] = None
    html_gz: Optional[builtins.bytes] = None  # `bytes` jest tu polem (liczba bajtów), więc typ bierzemy z builtins
    links: list = field(default_factory=list)  # LinkRecord
    contacts: list = field(default_factory=list)  # contacts.Contact
    counts: bool = True  # strona kosztowała żądanie sieciowe → liczy się do limitów firmy
