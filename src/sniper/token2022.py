"""Token-2022 TLV extension parsing.

pump.fun mints are Token-2022, not SPL Token (verified 60/60 on 2026-08-03) and
carry MetadataPointer + TokenMetadata. That puts name, symbol and uri **on
chain**, where they are free to read, batchable 100 per ``getMultipleAccounts``
call, and cannot be lost when a metadata host disappears.

Be clear about what this does *not* buy, because it is easy to overstate: the
socials live in the JSON document *at* that uri, on somebody else's server.
Reading the uri from chain makes the uri durable, not the document. Measured
2026-08-06, three days after collection, 5,784 of 16,197 recorded launches
(36%) already pointed at a host returning 404 - `metadata.j7tracker.io` alone
accounted for 5,181. That is why enrichment has to run near-real-time; see
``enrich/metadata.py``.

Layout is Borsh, per the SPL token-metadata-interface:

    update_authority     OptionalNonZeroPubkey  32 (all-zero means None)
    mint                 Pubkey                 32
    name                 String                 u32 length + utf8
    symbol               String                 u32 length + utf8
    uri                  String                 u32 length + utf8
    additional_metadata  Vec<(String, String)>  u32 count + pairs

Verified byte-for-byte against a real captured mint account: the three strings
decoded to ("Golden Duck", "Duck", an ipfs.io uri) and consumed the extension
body exactly, leaving 0 trailing bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import base58

# SPL Mint is a fixed 82-byte layout. Token-2022 reuses that prefix, pads to 165
# (so a mint can never be confused with a 165-byte token Account), puts an
# account_type discriminator at 165, then stores extensions as TLV records from
# 166: u16 type, u16 length, body.
MINT_ACCOUNT_SIZE = 82
TLV_ACCOUNT_TYPE_OFFSET = 165
TLV_START = 166

EXT_METADATA_POINTER = 18
EXT_TOKEN_METADATA = 19


@dataclass(frozen=True)
class TokenMetadata:
    """On-chain name/symbol/uri from the TokenMetadata extension."""

    update_authority: str | None
    mint: str
    name: str
    symbol: str
    uri: str
    additional: dict[str, str] = field(default_factory=dict)


def extension_records(data: bytes) -> list[tuple[int, bytes]]:
    """Walk the TLV region, returning (type, body) for each extension.

    A malformed or truncated record stops the walk rather than raising. A
    partial read is still worth reporting, and a mint account is untrusted
    third-party bytes - this must never be able to take down a caller.
    """
    if len(data) <= TLV_ACCOUNT_TYPE_OFFSET:
        return []

    out: list[tuple[int, bytes]] = []
    off = TLV_START
    while off + 4 <= len(data):
        ext_type, ext_len = struct.unpack_from("<HH", data, off)
        # A zero type with zero length is the end-of-list padding, not an
        # extension of type 0 (Uninitialized).
        if ext_type == 0 and ext_len == 0:
            break
        body = data[off + 4 : off + 4 + ext_len]
        if len(body) < ext_len:
            break  # truncated account; keep what we have
        out.append((ext_type, body))
        off += 4 + ext_len
    return out


def extension_types(data: bytes) -> list[int]:
    """Extension type ids present on a Token-2022 mint (empty for a base mint)."""
    return [t for t, _ in extension_records(data)]


def decode_token_metadata(data: bytes) -> TokenMetadata | None:
    """Decode the TokenMetadata extension, or None if absent/undecodable.

    Returns None rather than raising for anything malformed: a mint that does
    not carry readable metadata is an ordinary outcome to record, not an error
    that should abort a batch of 100.
    """
    for ext_type, body in extension_records(data):
        if ext_type != EXT_TOKEN_METADATA:
            continue
        try:
            return _parse_metadata_body(body)
        except (struct.error, UnicodeDecodeError, ValueError):
            return None
    return None


def _parse_metadata_body(body: bytes) -> TokenMetadata:
    pos = 0

    authority_bytes = body[pos : pos + 32]
    if len(authority_bytes) < 32:
        raise ValueError("truncated update_authority")
    pos += 32
    # OptionalNonZeroPubkey: all-zero is the None encoding, not a real pubkey.
    authority = (
        None if authority_bytes == bytes(32) else base58.b58encode(authority_bytes).decode()
    )

    mint_bytes = body[pos : pos + 32]
    if len(mint_bytes) < 32:
        raise ValueError("truncated mint")
    pos += 32
    mint = base58.b58encode(mint_bytes).decode()

    def read_string() -> str:
        nonlocal pos
        (length,) = struct.unpack_from("<I", body, pos)
        pos += 4
        raw = body[pos : pos + length]
        if len(raw) < length:
            raise ValueError("truncated string")
        pos += length
        # Token names are arbitrary user input; never let bad bytes raise.
        return raw.decode("utf-8", "replace")

    name = read_string()
    symbol = read_string()
    uri = read_string()

    additional: dict[str, str] = {}
    if pos + 4 <= len(body):
        (count,) = struct.unpack_from("<I", body, pos)
        pos += 4
        for _ in range(count):
            key = read_string()
            additional[key] = read_string()

    return TokenMetadata(
        update_authority=authority,
        mint=mint,
        name=name,
        symbol=symbol,
        uri=uri,
        additional=additional,
    )
