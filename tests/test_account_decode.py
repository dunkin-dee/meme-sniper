"""On-chain account decode tests.

Every fixture below is real account data fetched from Solana mainnet on
2026-08-03 via ``getMultipleAccounts`` and dumped verbatim (CLAUDE.md rule 4 -
a hand-reconstructed fixture once passed review and then failed against the
verified curve math, so these are byte-for-byte captures).

Two of these pin findings that contradicted the project's prior assumptions:

* pump.fun mints are **Token-2022**, not SPL Token.
* **mayhem-mode tokens violate the constant product**, so k-derived features are
  invalid for them.

If pump.fun changes either, these fail rather than silently corrupting features.
"""

import pytest

from sniper import curve
from sniper.verify import decode_mint, token_2022_extensions

# --------------------------------------------------------------------------
# Bonding curve: standard (non-mayhem)
#   mint  E5rkARFh2U4ZsXXCv7xxW34KwVrtzdpTPTjscuuwpump
#   curve 6PJXPfDPsEp4WnqKcPuqFZWoZnfC9mQWNQwHN8Z9hH4f
# Pristine: no trades yet, so vSol is exactly 30 and vTokens exactly 1.073e9.
# --------------------------------------------------------------------------
STANDARD_CURVE = bytes.fromhex(
    "17b7f83760d8ac600010d847e3cf030000ac23fc060000000078c5fb51d10200"
    "00000000000000000080c6a47e8d030000974df7af7fda4024bcd4a0cd325b8a"
    "5f54f9b60fd5a1791336f066954999ace3000000000000000000000000000000"
    "00000000000000000000000000000000000000"
)
STANDARD_CREATOR = "BBdWzyS9BTNCcTw7rTo9NgJctcvc6s54HgXMvEdCkobG"

# --------------------------------------------------------------------------
# Bonding curve: mayhem mode
#   mint  EYnWdg2hnNQCcJAvd4KWhhqzm4mZQbExVtRZoTTBpump
#   curve AZtKPVLxZeCmBhhU9Wg7PqFX8997xQivrSK4N7oSNtiU
# vSol 4.24 (below the 30 floor a standard curve can never breach) and vTokens
# 1.0788e9 (above the 1.073e9 opening reserve). k residual 8.6e-1.
# --------------------------------------------------------------------------
MAYHEM_CURVE = bytes.fromhex(
    "17b7f83760d8ac602de8455032d50300ba80bdfc000000002d503304a1d60200"
    "894a6f02000000000080c6a47e8d030000535f502d81a2680ee1774c569d3f10"
    "84179be0442564188360cf8276c5f5591f010000000000000000000000000000"
    "00000000000000000000000000000000000000"
)
MAYHEM_CREATOR = "7hXf1DZK3qMqBrC6BRETeFAedbpSXtq4xmmZUkYVwC6E"

# --------------------------------------------------------------------------
# Token-2022 mint account, 400 bytes
#   mint 5YE1wW1TkPZsdtLvNPsSQDBGGnpAXootFA3pVnkRpump  ("Golden Duck" / DuckC)
#   owner TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb
# --------------------------------------------------------------------------
MINT_ACCOUNT = bytes.fromhex(
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000008d49fd1a07000601000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000011200400000000000000000000000000000000000000000000000"
    "00000000000000000000436e786271596fbe12499b1b5f90b310eca87720ab9f"
    "de9e8d160645a57e246f1300a200000000000000000000000000000000000000"
    "0000000000000000000000000000436e786271596fbe12499b1b5f90b310eca8"
    "7720ab9fde9e8d160645a57e246f0b000000476f6c64656e204475636b040000"
    "004475636b4300000068747470733a2f2f697066732e696f2f697066732f516d"
    "514357374c6e646f437277396861767771477a6e7232674c31794261376b6f53"
    "476545666b4a5a427059655700000000"
)

DECODE = {"lamports_per_sol": 1_000_000_000, "token_decimals": 6}


# --------------------------------------------------------------------------
# Bonding curve layout
# --------------------------------------------------------------------------

def test_standard_curve_decodes_to_opening_reserves():
    bc = curve.decode_bonding_curve(STANDARD_CURVE, **DECODE)
    assert bc.virtual_sol == pytest.approx(curve.INITIAL_VIRTUAL_SOL)
    assert bc.virtual_tokens == pytest.approx(curve.INITIAL_VIRTUAL_TOKENS)
    assert bc.token_total_supply == pytest.approx(1_000_000_000.0)
    assert bc.complete is False
    assert bc.is_mayhem_mode is False


def test_creator_offset_matches_stream_recorded_creator():
    """The 32 bytes at offset 49 decode to the creator the stream reported.

    This is the load-bearing check on the whole layout: an exact 32-byte match
    at offset 49 can only happen if every preceding field is the right width.
    """
    assert curve.decode_bonding_curve(STANDARD_CURVE, **DECODE).creator == STANDARD_CREATOR


def test_creator_can_differ_from_the_transaction_signer():
    """On-chain ``creator`` is not always the stream's ``traderPublicKey``.

    Measured 2026-08-03: ~10-25% of launches decode to a creator that differs
    from the signer, consistently across samples. The layout is right (the rest
    match exactly), so this is a proxy/bundler deploying on someone's behalf -
    itself worth a feature. This fixture is one of them, which is why the
    assertion above is on the standard fixture only.
    """
    bc = curve.decode_bonding_curve(MAYHEM_CURVE, **DECODE)
    assert bc.creator != MAYHEM_CREATOR
    assert 32 <= len(bc.creator) <= 44  # well-formed base58 pubkey, not garbage


def test_standard_curve_satisfies_constant_product():
    state = curve.decode_bonding_curve(STANDARD_CURVE, **DECODE).to_state()
    assert state.k_residual() < 1e-9


def test_shifted_offset_is_caught_by_k_residual():
    """A one-byte misalignment must blow up k, not yield plausible numbers.

    This is why k_residual is the decode self-check: garbage that still parses
    is the failure mode worth defending against.
    """
    shifted = STANDARD_CURVE[:8] + b"\x00" + STANDARD_CURVE[8:]
    state = curve.decode_bonding_curve(shifted, **DECODE).to_state()
    assert state.k_residual() > 1e-3


def test_wrong_discriminator_rejected():
    with pytest.raises(curve.CurveError, match="discriminator"):
        curve.decode_bonding_curve(b"\x00" * 120, **DECODE)


def test_short_account_rejected():
    with pytest.raises(curve.CurveError, match="too short"):
        curve.decode_bonding_curve(STANDARD_CURVE[:40], **DECODE)


def test_account_is_over_allocated_beyond_its_fields():
    """Fields end at 84; pump.fun allocates more for forward compatibility.

    Observed sizes are 115 and 151 bytes, which is how cashback and mayhem mode
    were added without a migration. Never assert an exact account length.
    """
    assert len(STANDARD_CURVE) > curve.BONDING_CURVE_MIN_SIZE
    assert curve.decode_bonding_curve(STANDARD_CURVE, **DECODE).account_size == 115


# --------------------------------------------------------------------------
# Mayhem mode: a different curve mechanism, not a different constant
# --------------------------------------------------------------------------

def test_mayhem_curve_violates_constant_product():
    """Pins the 2026-08-03 finding (n=37, perfect separation).

    If this ever passes k, the mayhem/standard split has changed and every
    k-derived feature needs re-checking.
    """
    bc = curve.decode_bonding_curve(MAYHEM_CURVE, **DECODE)
    assert bc.is_mayhem_mode is True
    assert bc.to_state().k_residual() > 0.1


def test_mayhem_breaches_bounds_a_standard_curve_cannot():
    bc = curve.decode_bonding_curve(MAYHEM_CURVE, **DECODE)
    # Standard curves open at 30 SOL and only ever rise; tokens only ever fall.
    assert bc.virtual_sol < curve.INITIAL_VIRTUAL_SOL
    assert bc.virtual_tokens > curve.INITIAL_VIRTUAL_TOKENS


def test_standard_curve_applies_gate():
    assert curve.standard_curve_applies(False) is True
    assert curve.standard_curve_applies(True) is False
    # NULL is unknown, and unknown must not be treated as safe: the stream omits
    # is_mayhem_mode on a small share of frames.
    assert curve.standard_curve_applies(None) is False


# --------------------------------------------------------------------------
# Token-2022 mint
# --------------------------------------------------------------------------

def test_mint_authorities_are_revoked():
    mint = decode_mint(MINT_ACCOUNT)
    assert mint.mint_authority is None
    assert mint.freeze_authority is None
    assert mint.is_initialized is True
    assert mint.decimals == 6


def test_mint_carries_only_metadata_extensions():
    """18 = MetadataPointer, 19 = TokenMetadata.

    pump.fun uses Token-2022 for on-chain metadata, not for control over
    holders. Any of TransferFeeConfig / PermanentDelegate / TransferHook
    appearing here would mean per-token code that can tax or seize.
    """
    assert token_2022_extensions(MINT_ACCOUNT) == [18, 19]


def test_base_mint_reports_no_extensions():
    assert token_2022_extensions(MINT_ACCOUNT[:82]) == []
