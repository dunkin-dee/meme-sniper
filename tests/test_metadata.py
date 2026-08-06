"""Tier 1 metadata tests.

Every document below was fetched live from the URI recorded on a real launch on
2026-08-06 and pasted verbatim (CLAUDE.md rule 4). They are not illustrative
examples - each one pins a behaviour that a plausible-looking invented fixture
would have got wrong:

* ``LAUNCHBLITZ`` carries ``"website": ""`` and ``"telegram": ""``. Absent
  socials are empty strings, not missing keys. Testing key presence would score
  this token as having three socials instead of one and invert the very signal
  Tier 1 exists to measure.
* ``UXENTO`` spells it ``createdOn`` where ``LAUNCHBLITZ`` uses ``created_on``,
  which is why lookups are case-insensitive across spellings.
* ``BLOOMBOT`` has ``"description": ""`` - the same empty-string convention on a
  non-social field.
* ``IPFS_MINIMAL`` has no social keys at all, the common case.

The mint account is real Token-2022 bytes captured from mainnet, reused from
``test_account_decode`` rather than re-pasted.
"""

from test_account_decode import MINT_ACCOUNT

from sniper import token2022
from sniper.enrich.metadata import extract_socials, ipfs_candidates

# --------------------------------------------------------------------------
# Real metadata documents, fetched 2026-08-06.
# --------------------------------------------------------------------------

# https://ipfs.launchblitz.ai/... - the empty-string case.
LAUNCHBLITZ = {
    "name": "Antonio De Jesus Andrade-Ochoa",
    "symbol": "BEPE",
    "description": "-",
    "image": "https://ipfs.launchblitz.ai/async/0ddbd5a5-92cb-4f61-8cb1-7fa1d796f7e1.jpg",
    "show_name": True,
    "created_on": "launchblitz.ai",
    "twitter": "https://x.com/MikeyBSanders/status/2084406126301385100",
    "website": "",
    "telegram": "",
}

# https://meta.uxento.io/... - two socials, camelCase createdOn.
UXENTO = {
    "name": "ijustwannadance",
    "symbol": "dancecat",
    "image": "https://desperate-moccasin-minnow.myfilebase.com/ipfs/Qme49Y1E6Y9NcPhHR5t7W6kppdt9NzQwrpsHQYQFygKvgp",
    "createdOn": "https://pump.fun",
    "website": "https://www.tiktok.com/search?q=ijustwannadance&t=1785795758831",
    "twitter": "https://x.com/portom0x/status/2084406096970547500?s=20",
}

# https://metadata.bloombot.app/... - empty description.
BLOOMBOT = {
    "name": "Animal Meringues",
    "symbol": "MERINGUES",
    "description": "",
    "image": "https://ipfs.io/ipfs/bafybeiaxycrdlr37qoqm7kb6opgwqo52mtjqutvjzflyikygzdfcwxia54",
    "createdOn": "https://pump.fun",
    "twitter": "https://x.com/1Dnezia/status/2084011080716620132",
    "website": "https://www.tiktok.com/@a.wholestickofbutter/video/7667311620028321044",
}

# https://ipfs.io/ipfs/... - no socials at all.
IPFS_MINIMAL = {
    "image": "https://ipfs.io/ipfs/bafkreidvsrgqxp7qfkh7cejy4nhamroaos7la3co7fc6tkrmszsbzzhcja",
    "name": "thankyoutwin6t12",
    "symbol": "STIMMY",
}

GATEWAYS = [
    "https://ipfs.io/ipfs/",
    "https://cloudflare-ipfs.com/ipfs/",
    "https://gateway.pinata.cloud/ipfs/",
]


# --------------------------------------------------------------------------
# Socials extraction
# --------------------------------------------------------------------------

def test_empty_string_socials_count_as_absent():
    """The trap: "" means absent. Key presence would score this 3 instead of 1."""
    socials = extract_socials(LAUNCHBLITZ)
    assert socials.twitter == "https://x.com/MikeyBSanders/status/2084406126301385100"
    assert socials.website is None
    assert socials.telegram is None
    assert socials.count == 1


def test_two_socials_extracted():
    socials = extract_socials(UXENTO)
    assert socials.twitter is not None
    assert socials.website is not None
    assert socials.telegram is None
    assert socials.count == 2


def test_empty_description_is_none():
    socials = extract_socials(BLOOMBOT)
    assert socials.description is None
    assert socials.count == 2


def test_document_with_no_socials():
    socials = extract_socials(IPFS_MINIMAL)
    assert socials.count == 0
    assert (socials.twitter, socials.telegram, socials.website) == (None, None, None)
    assert socials.image is not None


def test_socials_lookup_is_case_insensitive():
    socials = extract_socials({"Twitter": "https://x.com/a", "TELEGRAM": "https://t.me/b"})
    assert socials.twitter == "https://x.com/a"
    assert socials.telegram == "https://t.me/b"
    assert socials.count == 2


def test_whitespace_only_social_is_absent():
    assert extract_socials({"twitter": "   "}).count == 0


def test_nested_extensions_are_merged():
    """Metaplex-style documents nest socials under `extensions`."""
    socials = extract_socials({"name": "x", "extensions": {"telegram": "https://t.me/c"}})
    assert socials.telegram == "https://t.me/c"


def test_top_level_wins_over_extensions():
    socials = extract_socials(
        {"telegram": "https://t.me/top", "extensions": {"telegram": "https://t.me/nested"}}
    )
    assert socials.telegram == "https://t.me/top"


def test_non_string_social_ignored():
    """Malformed documents must not crash or produce junk."""
    assert extract_socials({"twitter": 12345, "telegram": None}).count == 0


# --------------------------------------------------------------------------
# IPFS gateway fallback
# --------------------------------------------------------------------------

def test_ipfs_url_gets_gateway_alternatives():
    cid = "bafkreifksz7f2kqxu3yw5dz3727xggzt6552pjqrvn4ucpcgugncuquuhy"
    out = ipfs_candidates(f"https://ipfs.io/ipfs/{cid}", GATEWAYS)
    assert out[0] == f"https://ipfs.io/ipfs/{cid}"
    assert f"https://cloudflare-ipfs.com/ipfs/{cid}" in out
    assert len(out) == len(set(out))


def test_ipfs_scheme_is_expanded():
    out = ipfs_candidates("ipfs://QmAbc", GATEWAYS)
    assert out == [g.rstrip("/") + "/QmAbc" for g in GATEWAYS]


def test_plain_https_host_has_no_alternatives():
    """A 404 from a private host has no content-addressed fallback."""
    uri = "https://metadata.j7tracker.io/m/abc"
    assert ipfs_candidates(uri, GATEWAYS) == [uri]


# --------------------------------------------------------------------------
# On-chain Token-2022 metadata
# --------------------------------------------------------------------------

def test_token_metadata_decoded_from_real_mint():
    meta = token2022.decode_token_metadata(MINT_ACCOUNT)
    assert meta is not None
    assert meta.name == "Golden Duck"
    assert meta.symbol == "Duck"
    assert meta.uri.startswith("https://ipfs.io/ipfs/")
    assert meta.mint == "5YE1wW1TkPZsdtLvNPsSQDBGGnpAXootFA3pVnkRpump"
    # All-zero OptionalNonZeroPubkey means the authority is revoked, not that
    # some account with an all-zero key controls the metadata.
    assert meta.update_authority is None
    assert meta.additional == {}


def test_extension_records_expose_bodies():
    types = [t for t, _ in token2022.extension_records(MINT_ACCOUNT)]
    assert types == [token2022.EXT_METADATA_POINTER, token2022.EXT_TOKEN_METADATA]


def test_base_mint_has_no_metadata():
    assert token2022.decode_token_metadata(MINT_ACCOUNT[:82]) is None


def test_truncated_extension_body_returns_none():
    """Untrusted third-party bytes must never raise into a batch of 100."""
    assert token2022.decode_token_metadata(MINT_ACCOUNT[:200]) is None


def test_garbage_is_not_decoded():
    assert token2022.decode_token_metadata(b"\x00" * 400) is None
