from __future__ import annotations

import hashlib

import pytest

from ufrd_updater.hashes import (
    ALGORITHMS,
    HASH_KEYS,
    compute_hashes,
    hashes_from_digests,
    hashes_valid,
    nix32,
    sri,
)

ABC = {
    "md5": "900150983cd24fb0d6963f7d28e17f72",
    "md5-nix32": "3jgzhjhz9zjvbb0kyj7jc500ch",
    "md5-sri": "md5-kAFQmDzST7DWlj99KOF/cg==",
    "sha1": "a9993e364706816aba3e25717850c26c9cd0d89d",
    "sha1-nix32": "kpcd173cq987hw957sx6m0868wv3x6d9",
    "sha1-sri": "sha1-qZk+NkcGgWq6PiVxeFDCbJzQ2J0=",
    "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    "sha256-nix32": "1b8m03r63zqhnjf7l5wnldhh7c134ap5vpj0850ymkq1iyzicy5s",
    "sha256-sri": "sha256-ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=",
    "sha512": "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
    "sha512-nix32": "2gs8k559z4rlahfx0y688s49m2vvszylcikrfinm30ly9rak69236nkam5ydvly1ai7xac99vxfc4ii84hawjbk876blyk1jfhkbbyx",
    "sha512-sri": "sha512-3a81oZNherrMQXNJriBBMRLm+k6JqX6iCp7u5ktV05ohkpkqJ0/BqDa6PCOj/uu9RU1EI2Q86A4qmslPpUyknw==",
}


def test_abc_vectors():
    digests = {
        algorithm: hashlib.new(algorithm, b"abc").digest()
        for algorithm in ("md5", "sha1", "sha256", "sha512")
    }
    assert hashes_from_digests(digests) == ABC
    assert nix32(digests["sha256"]) == ABC["sha256-nix32"]
    assert sri("sha256", digests["sha256"]) == ABC["sha256-sri"]


def test_empty_sha256_trio():
    digest = hashlib.sha256(b"").digest()
    result = hashes_from_digests(
        {
            "md5": hashlib.md5(b"").digest(),
            "sha1": hashlib.sha1(b"").digest(),
            "sha256": digest,
            "sha512": hashlib.sha512(b"").digest(),
        }
    )
    assert digest.hex() == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert result["sha256-nix32"] == "0mdqa9w1p6cmli6976v4wi0sw9r4p5prkj7lzfd1877wk11c9c73"
    assert result["sha256-sri"] == "sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="


def test_streaming_file_matches_bytes(tmp_path):
    path = tmp_path / "data.bin"
    payload = b"abc" + b"x" * (2 * 1024 * 1024)
    path.write_bytes(payload)
    digests = {
        algorithm: hashlib.new(algorithm, payload).digest()
        for algorithm in ("md5", "sha1", "sha256", "sha512")
    }
    assert compute_hashes(path) == hashes_from_digests(digests)


@pytest.mark.parametrize("payload", [b"", b"abc", b"\x00" * 1025])
def test_complete_canonical_hashes_are_valid(payload):
    hashes = hashes_from_digests(
        {algorithm: hashlib.new(algorithm, payload).digest() for algorithm in ALGORITHMS}
    )

    assert hashes_valid(hashes) is True
    assert set(hashes) == set(HASH_KEYS)


@pytest.mark.parametrize("value", [None, "", 0, False, [], {}])
def test_non_hash_objects_are_invalid(value):
    assert hashes_valid(value) is False


@pytest.mark.parametrize("key", HASH_KEYS)
def test_missing_hash_key_is_invalid(key):
    hashes = dict(ABC)
    del hashes[key]

    assert hashes_valid(hashes) is False


def test_extra_hash_key_is_invalid():
    assert hashes_valid({**ABC, "unknown": "value"}) is False


@pytest.mark.parametrize("key", HASH_KEYS)
@pytest.mark.parametrize("value", ["", None, 1, False, [], {}, "not-a-digest"])
def test_invalid_hash_values_are_rejected(key, value):
    hashes = dict(ABC)
    hashes[key] = value

    assert hashes_valid(hashes) is False


@pytest.mark.parametrize("algorithm", ALGORITHMS)
@pytest.mark.parametrize("length_delta", [-1, 1])
def test_consistent_encodings_with_wrong_digest_length_are_invalid(algorithm, length_delta):
    digests = {
        name: hashlib.new(name, b"abc").digest()
        for name in ALGORITHMS
    }
    digests[algorithm] = b"\x00" * (len(digests[algorithm]) + length_delta)

    assert hashes_valid(hashes_from_digests(digests)) is False


@pytest.mark.parametrize(
    "key,value",
    [
        ("md5", ABC["md5"].upper()),
        ("sha256", "g" * 64),
        ("sha256-nix32", ABC["sha256-nix32"].upper()),
        ("sha256-nix32", "e" * 52),
        ("sha256-sri", ABC["sha256-sri"].rstrip("=")),
        ("sha256-sri", ABC["sha256-sri"][:-2] + "1="),
        ("sha256-sri", ABC["sha256-sri"].replace("sha256-", "sha512-")),
    ],
)
def test_noncanonical_or_malformed_hash_encoding_is_invalid(key, value):
    assert hashes_valid({**ABC, key: value}) is False


@pytest.mark.parametrize("algorithm", ALGORITHMS)
@pytest.mark.parametrize("suffix", ["", "-nix32", "-sri"])
def test_individually_valid_but_inconsistent_hash_encodings_are_invalid(algorithm, suffix):
    other = hashes_from_digests(
        {name: hashlib.new(name, b"different").digest() for name in ALGORITHMS}
    )
    key = f"{algorithm}{suffix}"

    assert hashes_valid({**ABC, key: other[key]}) is False
