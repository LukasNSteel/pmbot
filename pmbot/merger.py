"""On-chain YES+NO pair merging for live mode.

The CLOB has no merge endpoint — pairs are merged through Polymarket's pUSD
collateral adapters on Polygon (post the 2026-04 pUSD migration). The adapter
pulls equal YES+NO tokens from the wallet (one-time CTF setApprovalForAll),
merges them via the core CTF contract, and returns $1 pUSD per pair:

    standard markets:  CtfCollateralAdapter.mergePositions(
                           pUSD, 0x0, conditionId, [1, 2], amount)
    neg-risk markets:  NegRiskCtfCollateralAdapter.mergePositions(
                           conditionId, amount)

Who executes depends on the account type:
    signature_type 0 — the EOA holds the tokens; call the adapter directly.
    signature_type 1 — tokens live in a Polymarket proxy wallet; the owner
        EOA routes calls through ProxyWalletFactory.proxy(...), which executes
        them as the proxy wallet (approval + merge batch in one transaction).
    signature_type 2 — Gnosis Safe accounts are not supported here (needs a
        signed Safe transaction or Polymarket's relayer with builder
        credentials); pairs simply redeem at resolution instead.

Gas is paid in POL by the signing EOA. After repeated failures merging is
disabled for the session — the bot runs fine without it, pairs are riskless
($1 at resolution), just capital-locked.
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


def _calldata(signature: str, types: list[str], args: list) -> bytes:
    return keccak(text=signature)[:4] + abi_encode(types, args)


class Merger:
    """Builds, signs, and submits merge transactions over plain JSON-RPC."""

    def __init__(self, rpc_url: str, signature_type: int, private_key: str,
                 funder: str | None):
        self.rpc_url = rpc_url
        self.sig_type = signature_type
        self.account = Account.from_key(private_key)
        # The wallet that actually holds tokens/pUSD (proxy wallet for type 1).
        self.wallet = to_checksum_address(funder) if funder else self.account.address
        self.disabled: str | None = None
        self._approved: set[str] = set()
        self._failures = 0
        self._http = httpx.Client(timeout=20.0, verify=certifi.where())
        if self.sig_type not in (0, 1):
            self.disabled = ("signature_type 2 (Gnosis Safe) — on-chain merge "
                             "unsupported; pairs redeem at resolution instead")
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
        adapter = NEG_RISK_CTF_ADAPTER if neg_risk else CTF_ADAPTER
        if neg_risk:
            merge_call = _calldata("mergePositions(bytes32,uint256)",
                                   ["bytes32", "uint256"], [cid, amount])
        else:
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
