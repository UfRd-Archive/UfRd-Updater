"""Hash computation for UfRd-Updater.

Produces base16, Nix base32 and SRI forms for md5, sha1, sha256 and sha512.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from pathlib import Path

ALPHABET = "0123456789abcdfghijklmnpqrsvwxyz"

ALGORITHMS = ("md5", "sha1", "sha256", "sha512")
_DIGEST_SIZES = {"md5": 16, "sha1": 20, "sha256": 32, "sha512": 64}
CHUNK_SIZE = 1024 * 1024

# Fixed key order used by manifests, Nix output and release metadata.
HASH_KEYS = tuple(
    key
    for algorithm in ALGORITHMS
    for key in (algorithm, f"{algorithm}-nix32", f"{algorithm}-sri")
)


class HashError(Exception):
    """Raised when a digest cannot be formatted."""


def nix32(digest: bytes) -> str:
    """Encode a digest using Nix's base32 alphabet (little-endian bit order)."""

    n = (len(digest) * 8 - 1) // 5 + 1
    out: list[str] = []
    for i in range(n - 1, -1, -1):
        bit = i * 5
        byte, shift = divmod(bit, 8)
        value = digest[byte] >> shift
        if byte + 1 < len(digest):
            value |= digest[byte + 1] << (8 - shift)
        out.append(ALPHABET[value & 0x1F])
    return "".join(out)


def sri(algorithm: str, digest: bytes) -> str:
    """Encode a digest as a Nix SRI string."""

    return f"{algorithm}-{base64.b64encode(digest).decode('ascii')}"


def new_hashers() -> dict[str, "hashlib._Hash"]:
    """Create one hasher per supported algorithm."""

    return {algorithm: hashlib.new(algorithm) for algorithm in ALGORITHMS}


def hashes_from_digests(digests: Mapping[str, bytes]) -> dict[str, str]:
    """Build the twelve-key hash mapping from raw digests."""

    result: dict[str, str] = {}
    for algorithm in ALGORITHMS:
        try:
            digest = digests[algorithm]
        except KeyError as exc:  # pragma: no cover - defensive
            raise HashError(f"missing digest for {algorithm}") from exc
        result[algorithm] = digest.hex()
        result[f"{algorithm}-nix32"] = nix32(digest)
        result[f"{algorithm}-sri"] = sri(algorithm, digest)
    return result


def hashes_from_hashers(hashers: Mapping[str, "hashlib._Hash"]) -> dict[str, str]:
    """Finalize hashers into the twelve-key hash mapping."""

    return hashes_from_digests({name: hasher.digest() for name, hasher in hashers.items()})


def compute_hashes(path: str | Path) -> dict[str, str]:
    """Stream a file once and return all supported hash forms."""

    hashers = new_hashers()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            for hasher in hashers.values():
                hasher.update(chunk)
    return hashes_from_hashers(hashers)


def hashes_valid(hashes: object) -> bool:
    """Require all twelve canonical, mutually consistent hash encodings."""

    if (
        not isinstance(hashes, Mapping)
        or len(hashes) != len(HASH_KEYS)
        or any(key not in hashes for key in HASH_KEYS)
        or any(not isinstance(value, str) for value in hashes.values())
    ):
        return False
    digests: dict[str, bytes] = {}
    for algorithm in ALGORITHMS:
        value = hashes[algorithm]
        if len(value) != _DIGEST_SIZES[algorithm] * 2:
            return False
        try:
            digest = bytes.fromhex(value)
        except ValueError:
            return False
        if len(digest) != _DIGEST_SIZES[algorithm]:
            return False
        digests[algorithm] = digest
    return hashes == hashes_from_digests(digests)


def hashes_equal(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    """Compare all twelve supported hash values."""

    return all(left.get(key) == right.get(key) for key in HASH_KEYS)
