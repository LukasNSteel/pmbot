"""Tests for on-chain pair merging, focused on the POLY_1271 deposit-wallet
(signature_type 3) relayer path added for capital recycling."""

import pytest
from eth_utils import keccak, to_checksum_address

import py_builder_relayer_client.client as relayer_client_mod
import py_builder_signing_sdk.config as signing_config_mod
from pmbot.merger import (
    CTF,
    CTF_ADAPTER,
    NEG_RISK_CTF_ADAPTER,
    PUSD,
    USDC_DECIMALS,
    Merger,
)

PK = "0x" + "11" * 32
FUNDER = to_checksum_address("0x" + "ab" * 20)
CID = "0x" + "cd" * 32
RELAYER = "https://relayer.example"
CREDS = {"key": "k", "secret": "s", "passphrase": "p"}


def _selector(sig: str) -> str:
    return "0x" + keccak(text=sig)[:4].hex()


class _FakeResp:
    def __init__(self, confirmed):
        self.transaction_id = "tx-1"
        self._confirmed = confirmed

    def wait(self):
        return self._confirmed


class _FakeRelayClient:
    """Stand-in for py_builder_relayer_client RelayClient (no network)."""

    derived_wallet = FUNDER
    confirm = {"state": "STATE_CONFIRMED"}
    instances: list = []

    def __init__(self, url, chain_id, private_key, config, rpc_url=None):
        self.url = url
        self.chain_id = chain_id
        self.batches: list = []
        _FakeRelayClient.instances.append(self)

    def get_expected_deposit_wallet(self):
        return self.derived_wallet

    def get_nonce(self, address, signer_type):
        return {"nonce": 8}

    def execute_deposit_wallet_batch(self, calls, wallet, nonce, deadline):
        self.batches.append(
            {"calls": calls, "wallet": wallet, "nonce": nonce, "deadline": deadline}
        )
        return _FakeResp(self.confirm)


@pytest.fixture
def patch_relayer(monkeypatch):
    _FakeRelayClient.instances = []
    _FakeRelayClient.derived_wallet = FUNDER
    _FakeRelayClient.confirm = {"state": "STATE_CONFIRMED"}
    monkeypatch.setattr(relayer_client_mod, "RelayClient", _FakeRelayClient)
    # BuilderConfig / BuilderApiKeyCreds are imported inside _init_relayer; keep
    # them as no-op stand-ins so we don't depend on their validation.
    monkeypatch.setattr(signing_config_mod, "BuilderApiKeyCreds",
                        lambda **kw: kw)
    monkeypatch.setattr(signing_config_mod, "BuilderConfig",
                        lambda **kw: object())
    return _FakeRelayClient


def _merger(**kw):
    return Merger("http://rpc", 3, PK, FUNDER, relayer_url=RELAYER, **kw)


# --------------------------------------------------------------- gating

def test_deposit_wallet_without_creds_is_disabled():
    m = Merger("http://rpc", 3, PK, FUNDER)
    assert m.disabled is not None
    assert "RELAYER" in m.disabled.upper()
    assert m.merge(CID, neg_risk=False, pairs=50) is False


def test_safe_type_still_disabled():
    m = Merger("http://rpc", 2, PK, FUNDER)
    assert m.disabled is not None
    assert "Safe" in m.disabled


def test_relayer_enabled_with_creds(patch_relayer):
    m = _merger(builder_creds=CREDS)
    assert m.disabled is None
    assert m._relayer is not None


def test_relayer_disabled_on_wallet_mismatch(patch_relayer):
    patch_relayer.derived_wallet = to_checksum_address("0x" + "cc" * 20)
    m = _merger(builder_creds=CREDS)
    assert m.disabled is not None
    assert "!=" in m.disabled


# --------------------------------------------------------------- batch shape

def test_standard_merge_builds_collateral_adapter_call(patch_relayer):
    m = _merger(builder_creds=CREDS)
    m._eth_call = lambda to, data: "0x" + "0" * 63 + "1"  # already approved
    assert m.merge(CID, neg_risk=False, pairs=50) is True

    batch = patch_relayer.instances[-1].batches[-1]
    assert batch["wallet"] == FUNDER
    assert len(batch["calls"]) == 1
    call = batch["calls"][0]
    assert call.target == CTF_ADAPTER
    assert call.value == "0"
    assert call.data.startswith(
        _selector("mergePositions(address,bytes32,bytes32,uint256[],uint256)"))


def test_neg_risk_merge_uses_neg_risk_adapter(patch_relayer):
    m = _merger(builder_creds=CREDS)
    m._eth_call = lambda to, data: "0x" + "0" * 63 + "1"  # already approved
    assert m.merge(CID, neg_risk=True, pairs=30) is True

    call = patch_relayer.instances[-1].batches[-1]["calls"][0]
    assert call.target == NEG_RISK_CTF_ADAPTER
    # Neg-risk uses the SAME pUSD-native 5-arg signature as standard markets —
    # only the adapter address differs. The 2-arg mergePositions(bytes32,uint256)
    # is the core NegRiskAdapter's USDC.e path and reverts on this adapter.
    assert call.data.startswith(
        _selector("mergePositions(address,bytes32,bytes32,uint256[],uint256)"))


def test_unapproved_prepends_setapprovalforall(patch_relayer):
    m = _merger(builder_creds=CREDS)
    m._eth_call = lambda to, data: "0x" + "0" * 64  # not approved
    assert m.merge(CID, neg_risk=False, pairs=10) is True

    calls = patch_relayer.instances[-1].batches[-1]["calls"]
    assert len(calls) == 2
    assert calls[0].target == CTF
    assert calls[0].data.startswith(_selector("setApprovalForAll(address,bool)"))
    assert calls[1].target == CTF_ADAPTER


def test_relayer_non_confirmation_fails_and_counts(patch_relayer):
    patch_relayer.confirm = None  # relayer never confirms
    m = _merger(builder_creds=CREDS)
    m._eth_call = lambda to, data: "0x" + "0" * 63 + "1"
    assert m.merge(CID, neg_risk=False, pairs=10) is False
    assert m._failures == 1


def test_bad_condition_id_rejected(patch_relayer):
    m = _merger(builder_creds=CREDS)
    assert m.merge("0xdeadbeef", neg_risk=False, pairs=10) is False
