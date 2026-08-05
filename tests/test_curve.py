"""Curve math tests.

The fixtures are real create frames captured from the live PumpPortal stream on
2026-08-02, not synthetic values. If pump.fun changes the curve parameters these
tests are the tripwire.
"""

import pytest

from sniper import curve

# Verbatim (vSolInBondingCurve, vTokensInBondingCurve, solAmount) triples dumped
# from the recorder's database, chosen to span the observed pre-buy range
# (0 SOL to 11.85 SOL). Worst k residual across all 53 captured rows: 1.2e-16.
LIVE_FRAMES = [
    (41.85185184999999, 769141592.954387, 11.85185185),   # starhorse
    (36.91358024499999, 872036789.342865, 6.913580245),   # HORSE
    (30.493827157999995, 1055623481.867707, 0.493827158),  # ICELANDIC
    (30.00098765299999, 1072964676.107292, 0.000987653),  # TNOS
    (30.0, 1073000000.0, 0.0),                             # THOGE, no pre-buy
]


@pytest.mark.parametrize("vsol,vtokens,prebuy", LIVE_FRAMES)
def test_constant_product_holds_on_live_frames(vsol, vtokens, prebuy):
    """k = vSol * vTokens is invariant across every observed launch."""
    assert curve.K == pytest.approx(vsol * vtokens, rel=1e-9)


@pytest.mark.parametrize("vsol,vtokens,prebuy", LIVE_FRAMES)
def test_create_frame_reports_post_prebuy_state(vsol, vtokens, prebuy):
    """vSol == 30 + dev pre-buy, exactly."""
    assert vsol == pytest.approx(curve.INITIAL_VIRTUAL_SOL + prebuy, abs=1e-6)
    assert curve.dev_prebuy_sol(vsol) == pytest.approx(prebuy, abs=1e-6)


@pytest.mark.parametrize("vsol,vtokens,prebuy", LIVE_FRAMES)
def test_tokens_for_sol_reproduces_observed_reserve(vsol, vtokens, prebuy):
    """Deriving the token reserve from k alone matches the observed value."""
    assert curve.tokens_for_sol(vsol) == pytest.approx(vtokens, rel=1e-9)


def test_k_matches_documented_initial_reserves():
    assert curve.K == pytest.approx(3.219e10, rel=1e-6)


def test_progress_is_zero_at_launch_and_full_at_graduation():
    assert curve.progress_pct(curve.INITIAL_VIRTUAL_SOL) == 0.0
    assert curve.progress_pct(curve.GRADUATION_VIRTUAL_SOL) == pytest.approx(100.0)


def test_progress_clamps_outside_the_curve():
    assert curve.progress_pct(10.0) == 0.0
    assert curve.progress_pct(500.0) == 100.0


def test_large_prebuy_advances_the_curve_at_t0():
    """An 11.85 SOL pre-buy buys ~13.9% of the curve before anyone else can."""
    assert curve.progress_pct(41.85185185) == pytest.approx(13.94, abs=0.01)


def test_sol_to_graduate():
    assert curve.sol_to_graduate(30.0) == pytest.approx(85.0)
    assert curve.sol_to_graduate(115.0) == 0.0
    assert curve.sol_to_graduate(200.0) == 0.0


def test_buy_and_sell_quotes_are_inverses():
    """tokens_out_for_sol and buy_cost_sol must round-trip."""
    vsol = 45.0
    sol_in = 2.5
    tokens = curve.tokens_out_for_sol(vsol, sol_in)
    assert curve.buy_cost_sol(vsol, tokens) == pytest.approx(sol_in, rel=1e-9)


def test_price_rises_monotonically_along_the_curve():
    prices = [curve.price_sol_per_token(v) for v in (30, 45, 60, 85, 115)]
    assert prices == sorted(prices)
    # Graduation price is ~14.7x the launch price - the full bonding-curve move.
    assert prices[-1] / prices[0] == pytest.approx(14.69, abs=0.1)


def test_price_uses_observed_reserve_when_supplied():
    """A real on-chain reading must not be silently overridden by ideal k."""
    off_curve = curve.price_sol_per_token(45.0, 1_000_000.0)
    assert off_curve == pytest.approx(45.0 / 1_000_000.0)


def test_curve_state_k_residual_flags_bad_decodes():
    good = curve.CurveState(virtual_sol=45.0, virtual_tokens=curve.tokens_for_sol(45.0))
    assert good.k_residual() < 1e-12

    # A byte-offset bug would produce nonsense reserves; residual must be large.
    bad = curve.CurveState(virtual_sol=45.0, virtual_tokens=1_000_000.0)
    assert bad.k_residual() > 0.5


def test_rejects_impossible_state():
    with pytest.raises(curve.CurveError):
        curve.tokens_for_sol(0.0)
    with pytest.raises(curve.CurveError):
        curve.tokens_out_for_sol(45.0, -1.0)
    with pytest.raises(curve.CurveError):
        curve.buy_cost_sol(45.0, curve.tokens_for_sol(45.0) + 1)
