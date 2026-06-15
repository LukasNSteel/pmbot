"""On-chain YES+NO pair merging for live mode.

The CLOB has no merge endpoint — pairs are merged through Polymarket's pUSD
collateral adapters on Polygon (post the 2026-04 pUSD migration). The adapter
pulls equal YES+NO tokens from the wallet (one-time CTF setApprovalForAll),
merges them via the core CTF contract, receives the released USDC.e, wraps it
into pUSD, and returns $1 pUSD per pair. Both market types use the SAME
pUSD-native 5-arg signature — only the adapter address differs:

    standard markets:  CtfCollateralAdapter.mergePositions(
                           pUSD, 0x0, conditionId, [1, 2], amount)
    neg-risk markets:  NegRiskCtfCollateralAdapter.mergePositions(
                           pUSD, 0x0, conditionId, [1, 2], amount)

Who executes depends on the account type:
    signature_type 0 — the EOA holds the tokens; call the adapter directly,
        paying POL gas from the EOA.
    signature_type 1 — tokens live in a Polymarket proxy wallet; the owner
        EOA routes calls through ProxyWalletFactory.proxy(...), which executes
        them as the proxy wallet (approval + merge batch in one transaction),
        paying POL gas from the EOA.
    signature_type 3 — tokens live in a POLY_1271 deposit wallet (an ERC-1967
        proxy whose execute() is onlyFactory, so it cannot be self-submitted).
        The owner signs a DepositWallet `Batch` (EIP-712) and POSTs it to
        Polymarket's relayer, whose operator forwards it via the factory. This
        path is GASLESS (the relayer pays) but needs builder API-key creds
        (Polymarket Settings -> API Keys). Without creds, merging stays
        disabled and pairs redeem at resolution.
    signature_type 2 — Gnosis Safe accounts are not supported here (needs a
        signed Safe transaction); pairs simply redeem at resolution instead.

After repeated failures merging is disabled for the session — the bot runs
fine without it, pairs are riskless ($1 at resolution), just capital-locked.
"""

from __future__ import annotations

import logging
import time

import certifi
import httpx
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

log = logging.getLogger("pmbot.merger")

# Verified against https://docs.polymarket.com/resources/contracts and
# on-chain bytecode (function selectors) on Polygon mainnet.
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
CTF_ADAPTER = "0xAdA100Db00Ca00073811820692005400218FcE1f"
NEG_RISK_CTF_ADAPTER = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"
PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
PROXY_CALL_TYPE_CALL = 1  # ProxyWalletLib.CallType.Call

CHAIN_ID = 137
USDC_DECIMALS = 10**6
GAS_PRICE_CAP_GWEI = 1000.0
RECEIPT_POLL_SECS = 3.0
RECEIPT_TIMEOUT_SECS = 120.0
MAX_FAILURES = 3

# Deposit-wallet (signature_type 3) gasless merging via Polymarket's relayer.
DEFAULT_RELAYER_URL = "https://relayer-v2.polymarket.com"
RELAYER_BATCH_DEADLINE_SECS = 600


def _calldata(signature: str, types: list[str], args: list) -> bytes:
    return keccak(text=signature)[:4] + abi_encode(types, args)


class Merger:
    """Builds, signs, and submits merge transactions over plain JSON-RPC."""

    def __init__(self, rpc_url: str, signature_type: int, private_key: str,
                 funder: str | None, relayer_url: str | None = None,
                 builder_creds: dict | None = None):
        self.rpc_url = rpc_url
        self.sig_type = signature_type
        self._private_key = private_key
        self.account = Account.from_key(private_key)
        # The wallet that actually holds tokens/pUSD (proxy / deposit wallet).
        self.wallet = to_checksum_address(funder) if funder else self.account.address
        self.disabled: str | None = None
        self._approved: set[str] = set()
        self._failures = 0
        self._http = httpx.Client(timeout=20.0, verify=certifi.where())
        self._relayer = None  # set for signature_type 3 when creds are present
        if self.sig_type in (0, 1):
            pass  # self-submitted JSON-RPC path (gas paid by the EOA)
        elif self.sig_type == 3:
            if not builder_creds:
                self.disabled = (
                    "POLY_1271 deposit wallet — set relayer builder API creds "
                    "(POLYMARKET_BUILDER_API_KEY / _SECRET / _PASSPHRASE) to enable "
                    "gasless on-chain merging; until then pairs redeem at resolution")
                log.info("merging disabled: %s", self.disabled)
            else:
                try:
                    self._init_relayer(relayer_url or DEFAULT_RELAYER_URL, builder_creds)
                    log.info("deposit-wallet merging enabled via relayer (wallet %s)",
                             self.wallet)
                except Exception as e:  # noqa: BLE001
                    self.disabled = f"deposit-wallet relayer init failed: {e}"
                    log.warning("merging disabled: %s", self.disabled)
        else:
            self.disabled = ("Gnosis Safe — on-chain merge unsupported here; "
                             "pairs redeem at resolution instead")
            log.info("merging disabled: %s", self.disabled)

    # ------------------------------------------------------------ JSON-RPC

    def _rpc(self, method: str, params: list):
        resp = self._http.post(self.rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        })
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error']}")
        return data["result"]

    def _eth_call(self, to: str, data: bytes) -> str:
        return self._rpc("eth_call", [{"to": to, "data": "0x" + data.hex()}, "latest"])

    def _send_tx(self, to: str, data: bytes) -> bool:
        """Sign and submit one transaction; True once mined successfully.
        eth_estimateGas doubles as a simulation — reverts surface there,
        before any gas is spent."""
        sender = self.account.address
        tx_probe = {"from": sender, "to": to, "data": "0x" + data.hex()}
        gas = int(self._rpc("eth_estimateGas", [tx_probe]), 16)
        gas_price = int(int(self._rpc("eth_gasPrice", []), 16) * 1.25)
        if gas_price > GAS_PRICE_CAP_GWEI * 1e9:
            raise RuntimeError(f"gas price {gas_price / 1e9:.0f} gwei over cap")
        nonce = int(self._rpc("eth_getTransactionCount", [sender, "pending"]), 16)
        signed = self.account.sign_transaction({
            "chainId": CHAIN_ID, "nonce": nonce, "to": to, "value": 0,
            "data": data, "gas": int(gas * 1.3), "gasPrice": gas_price,
        })
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = self._rpc("eth_sendRawTransaction", ["0x" + raw.hex().removeprefix("0x")])
        log.info("merge tx sent: %s", tx_hash)
        deadline = time.time() + RECEIPT_TIMEOUT_SECS
        while time.time() < deadline:
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt is not None:
                ok = int(receipt["status"], 16) == 1
                if not ok:
                    raise RuntimeError(f"tx reverted on-chain: {tx_hash}")
                return True
            time.sleep(RECEIPT_POLL_SECS)
        raise RuntimeError(f"timed out waiting for receipt: {tx_hash}")

    # ------------------------------------------------------------ calls

    def _ensure_approval(self, adapter: str) -> list[tuple[str, bytes]]:
        """CTF setApprovalForAll(adapter) from the token-holding wallet,
        once per adapter. Returns the call to prepend, or [] if approved."""
        if adapter in self._approved:
            return []
        probe = _calldata("isApprovedForAll(address,address)",
                          ["address", "address"], [self.wallet, adapter])
        if int(self._eth_call(CTF, probe), 16) == 1:
            self._approved.add(adapter)
            return []
        log.info("CTF approval needed for adapter %s…", adapter[:10])
        return [(CTF, _calldata("setApprovalForAll(address,bool)",
                                ["address", "bool"], [adapter, True]))]

    def _execute(self, calls: list[tuple[str, bytes]]) -> bool:
        if self.sig_type == 3:
            return self._execute_via_relayer(calls)
        if self.sig_type == 1:
            # Route through the proxy factory so the calls execute as the
            # proxy wallet; the whole batch lands in one transaction.
            proxy_calls = [(PROXY_CALL_TYPE_CALL, to, 0, data) for to, data in calls]
            data = _calldata("proxy((uint8,address,uint256,bytes)[])",
                             ["(uint8,address,uint256,bytes)[]"], [proxy_calls])
            return self._send_tx(PROXY_FACTORY, data)
        for to, data in calls:  # EOA: sequential transactions
            if not self._send_tx(to, data):
                return False
        return True

    # ----------------------------------------------------- deposit wallet (3)

    def _init_relayer(self, relayer_url: str, builder_creds: dict) -> None:
        """Build the Polymarket relayer client for the deposit-wallet flow.

        Raises if creds are malformed or the relayer-derived deposit wallet
        disagrees with the configured funder (clear misconfiguration)."""
        from py_builder_relayer_client.client import RelayClient
        from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig

        config = BuilderConfig(local_builder_creds=BuilderApiKeyCreds(
            key=builder_creds["key"], secret=builder_creds["secret"],
            passphrase=builder_creds["passphrase"]))
        self._relayer = RelayClient(relayer_url, CHAIN_ID, self._private_key,
                                    config, rpc_url=self.rpc_url)
        # Sanity: the deterministic deposit wallet must match the funder. A
        # mismatch means orders and merges would target different wallets.
        try:
            derived = to_checksum_address(self._relayer.get_expected_deposit_wallet())
        except Exception as e:  # noqa: BLE001 — network/derivation hiccup, non-fatal
            log.debug("could not derive deposit wallet for sanity check: %s", e)
            return
        if derived != self.wallet:
            raise RuntimeError(
                f"relayer-derived deposit wallet {derived} != funder {self.wallet}")

    def _execute_via_relayer(self, calls: list[tuple[str, bytes]]) -> bool:
        """Submit the approval+merge calls as one signed DepositWallet Batch to
        the relayer and block until it confirms on-chain. Gasless."""
        from py_builder_relayer_client.models import DepositWalletCall, TransactionType

        dw_calls = [
            DepositWalletCall(target=to_checksum_address(to), value="0",
                              data="0x" + data.hex())
            for to, data in calls
        ]
        nonce_payload = self._relayer.get_nonce(self.account.address,
                                                TransactionType.WALLET.value)
        nonce = nonce_payload.get("nonce") if nonce_payload else None
        if nonce is None:
            raise RuntimeError("relayer returned no WALLET nonce")
        deadline = str(int(time.time()) + RELAYER_BATCH_DEADLINE_SECS)
        resp = self._relayer.execute_deposit_wallet_batch(
            dw_calls, self.wallet, str(nonce), deadline)
        log.info("relayer batch submitted (txID=%s); awaiting confirmation…",
                 resp.transaction_id)
        confirmed = resp.wait()
        if confirmed is None:
            raise RuntimeError("relayer batch did not confirm (failed or timed out)")
        return True

    def merge(self, condition_id: str, neg_risk: bool, pairs: float) -> bool:
        """Merge `pairs` YES+NO pairs back into pUSD. Returns True on success."""
        if self.disabled:
            return False
        cid = bytes.fromhex(condition_id.removeprefix("0x"))
        if len(cid) != 32:
            log.error("bad condition id %s", condition_id)
            return False
        amount = int(pairs) * USDC_DECIMALS
        if amount <= 0:
            return False
        # Both the standard and neg-risk collateral adapters expose the same
        # pUSD-native CTF signature; only the target contract differs. Each
        # adapter merges via the core CTF, takes the released USDC.e, and wraps
        # it back into pUSD for the wallet. (The neg-risk adapter does NOT use a
        # 2-arg mergePositions(conditionId, amount) — that selector exists but is
        # the core NegRiskAdapter's USDC.e path and reverts here.)
        adapter = NEG_RISK_CTF_ADAPTER if neg_risk else CTF_ADAPTER
        merge_call = _calldata(
            "mergePositions(address,bytes32,bytes32,uint256[],uint256)",
            ["address", "bytes32", "bytes32", "uint256[]", "uint256"],
            [PUSD, b"\x00" * 32, cid, [1, 2], amount])
        try:
            calls = self._ensure_approval(adapter) + [(adapter, merge_call)]
            ok = self._execute(calls)
        except Exception as e:  # noqa: BLE001
            self._failures += 1
            log.error("on-chain merge failed (%d/%d): %s",
                      self._failures, MAX_FAILURES, e)
            if self._failures >= MAX_FAILURES:
                self.disabled = f"{self._failures} consecutive failures (last: {e})"
                log.error("merging disabled for this session — pairs will "
                          "redeem at resolution instead")
            return False
        if ok:
            self._failures = 0
            self._approved.add(adapter)
        return ok
