"""Deterministic well-formed CIDs for tests that go through ingest/publish."""
from __future__ import annotations

import base64
import hashlib


def cid_for(label: str) -> str:
    """CIDv1 raw/sha2-256 of ``label``. Decodes with observer.cidutil."""
    digest = hashlib.sha256(label.encode()).digest()
    raw = bytes([0x01, 0x55, 0x12, 0x20]) + digest
    return "b" + base64.b32encode(raw).decode().lower().rstrip("=")


def cid_pb_for(label: str) -> str:
    """CIDv1 dag-pb/sha2-256 of ``label``."""
    digest = hashlib.sha256(label.encode()).digest()
    raw = bytes([0x01, 0x70, 0x12, 0x20]) + digest
    return "b" + base64.b32encode(raw).decode().lower().rstrip("=")
