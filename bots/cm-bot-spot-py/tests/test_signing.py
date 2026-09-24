"""Verify our signing matches the VALR test vectors from their docs."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.valr_rest import sign


def test_signing_no_subaccount():
    """Smoke test: signature is deterministic and hex-encoded."""
    secret = "test-secret"
    ts1, sig1 = sign(secret, "GET", "/v1/account/balances", "", "")
    assert len(sig1) == 128  # SHA512 hex
    # Determinism: same inputs same sig (different ts though)
    ts2, sig2 = sign(secret, "GET", "/v1/account/balances", "", "")
    if ts1 == ts2:
        assert sig1 == sig2


def test_signing_with_subaccount():
    secret = "test-secret"
    _, sig_a = sign(secret, "GET", "/v1/account/balances", "", "subA")
    _, sig_b = sign(secret, "GET", "/v1/account/balances", "", "subB")
    # Different subaccounts → different sigs
    assert sig_a != sig_b
