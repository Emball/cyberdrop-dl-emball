"""Parse content hashes from HTTP response headers.

Many file hosts serve a hash of the content in response headers, giving us
a zero-byte duplicate signal before any content is downloaded.

Supported header formats:
  - Content-MD5          (RFC 1864)  base64-encoded MD5
  - Digest               (RFC 3230)  "algorithm=base64value" or "sha-256=..."
  - x-goog-hash          (GCS)       "md5=base64, crc32c=base64"
  - x-amz-checksum-sha256 / x-amz-checksum-crc32  (AWS S3)  base64-encoded
  - ETag                             raw hex if it looks like a complete MD5/SHA256
  - x-content-hash                   some CDNs emit this as plain hex
  - x-bz-content-sha1                Backblaze B2

The returned (hash_type, hex_value) tuples are normalised to lowercase hex
so they match what we store in the hash table.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

# Maps (header_name, algorithm_hint) → canonical hash_type name used in our DB
_ALGORITHM_MAP: dict[str, str] = {
    "md5": "md5",
    "sha-256": "sha256",
    "sha256": "sha256",
    "sha-1": "sha1",
    "sha1": "sha1",
    "crc32c": "crc32c",
    "crc32": "crc32",
}

# Hex lengths → algorithm name (for bare-hex ETag / x-content-hash detection)
_HEX_LENGTH_TO_ALGO: dict[int, str] = {
    32: "md5",
    40: "sha1",
    64: "sha256",
}

_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+=*$")


def _b64_to_hex(value: str) -> str | None:
    """Decode a base64 string to lowercase hex.  Returns None on failure."""
    try:
        raw = base64.b64decode(value)
        return binascii.hexlify(raw).decode()
    except Exception:
        return None


def _looks_like_hex(value: str) -> bool:
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


def _parse_digest_header(value: str) -> list[tuple[str, str]]:
    """Parse RFC 3230 Digest header.

    Format: 'SHA-256=base64value' or comma-separated multiples.
    Returns list of (hash_type, hex_value).
    """
    results: list[tuple[str, str]] = []
    for part in value.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        algo_raw, _, b64 = part.partition("=")
        algo_raw = algo_raw.strip().lower()
        b64 = b64.strip()
        algo = _ALGORITHM_MAP.get(algo_raw)
        if not algo:
            continue
        hex_val = _b64_to_hex(b64)
        if hex_val:
            results.append((algo, hex_val))
    return results


def _parse_goog_hash_header(value: str) -> list[tuple[str, str]]:
    """Parse x-goog-hash header.

    Format: 'md5=base64value, crc32c=base64value'
    """
    results: list[tuple[str, str]] = []
    for part in value.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        algo_raw, _, b64 = part.partition("=")
        algo_raw = algo_raw.strip().lower()
        b64 = b64.strip()
        algo = _ALGORITHM_MAP.get(algo_raw)
        if not algo:
            continue
        hex_val = _b64_to_hex(b64)
        if hex_val:
            results.append((algo, hex_val))
    return results


def extract_hashes(headers: Mapping[str, str]) -> list[tuple[str, str]]:
    """Extract all usable (hash_type, hex_value) pairs from response headers.

    Returns a list ordered by signal strength (strongest first):
      sha256 > md5 > sha1 > crc32c > etag-derived
    """
    results: list[tuple[str, str]] = []

    # 1. Digest header (RFC 3230) — strongest, algorithm is explicit
    if digest := headers.get("Digest"):
        results.extend(_parse_digest_header(digest))

    # 2. x-goog-hash (GCS)
    if goog := headers.get("x-goog-hash"):
        results.extend(_parse_goog_hash_header(goog))

    # 3. AWS S3 checksum headers (base64-encoded)
    for amz_header, algo in (
        ("x-amz-checksum-sha256", "sha256"),
        ("x-amz-checksum-sha1", "sha1"),
        ("x-amz-checksum-crc32", "crc32"),
        ("x-amz-checksum-crc32c", "crc32c"),
    ):
        if val := headers.get(amz_header):
            hex_val = _b64_to_hex(val.strip())
            if hex_val:
                results.append((algo, hex_val))

    # 4. Content-MD5 (RFC 1864) — base64 MD5
    if cmd5 := headers.get("Content-MD5"):
        hex_val = _b64_to_hex(cmd5.strip())
        if hex_val and len(hex_val) == 32:
            results.append(("md5", hex_val))

    # 5. Backblaze B2
    if b2sha := headers.get("x-bz-content-sha1"):
        val = b2sha.strip()
        if len(val) == 40 and _looks_like_hex(val):
            results.append(("sha1", val.lower()))

    # 6. x-content-hash (various CDNs) — raw hex
    if xch := headers.get("x-content-hash"):
        val = xch.strip().lower()
        if val in ("", "none"):
            pass
        elif algo := _HEX_LENGTH_TO_ALGO.get(len(val)):
            if _looks_like_hex(val):
                results.append((algo, val))

    # 7. ETag — only use if it's a bare hex MD5 or SHA256 (no quotes, no weak prefix)
    if etag_raw := headers.get("ETag", ""):
        etag_val = etag_raw.strip().strip('"')
        if not etag_val.startswith("W/"):
            etag_val = etag_val.lower()
            if algo := _HEX_LENGTH_TO_ALGO.get(len(etag_val)):
                if _looks_like_hex(etag_val):
                    # Only add if not already covered by a stronger header
                    already_have = {h for h, _ in results}
                    if algo not in already_have:
                        results.append((algo, etag_val))

    # Deduplicate while preserving order
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for item in results:
        if item not in seen:
            seen.add(item)
            unique.append(item)

    if unique:
        logger.debug("Extracted header hashes: %s", [(t, v[:12] + "...") for t, v in unique])

    return unique
