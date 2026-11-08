"""Guarded token sells on Robinhood Chain via Uniswap V3 (SwapRouter02).

Preparation is read-only. execute_sell broadcasts only after the Telegram
final-confirm callback; it first broadcasts the ERC-20 approval (if needed),
waits for it, then signs and broadcasts the swap.
"""
import time
from decimal import Decimal, InvalidOperation
from eth_abi import encode, decode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from acobot.tokenbuy import (CHAIN_ID, SWAP_ROUTER_02, QUOTER_V2, WETH, FEE,
                             DEFAULT_SLIPPAGE_BPS, MAX_SLIPPAGE_BPS,
                             _selector, _addr, _int)

APPROVE_SELECTOR = "approve(address,uint256)"
BALANCE_OF_SELECTOR = "balanceOf(address)"
DECIMALS_SELECTOR = "decimals()"


def _erc20_string(token, rpc, selector):
    try:
        raw = rpc.call("eth_call", [{"to": token, "data": "0x" + _selector(selector).hex()}, "latest"])
        body = bytes.fromhex(raw[2:])
        if len(body) >= 64:
            length = int.from_bytes(body[0:32], "big")
            return body[32:32 + length].decode("utf-8", "ignore")
    except Exception:
        pass
    return ""


def token_meta(token, rpc):
    dec = 18
    try:
        raw = rpc.call("eth_call", [{"to": token, "data": "0x" + _selector(DECIMALS_SELECTOR).hex()}, "latest"])
        dec = _int(raw)
    except Exception:
        pass
    if not 0 < dec <= 36:
        dec = 18
    return dec, _erc20_string(token, rpc, "symbol()") or "TOKEN"


def token_balance_raw(token, wallet, rpc):
    data = "0x" + (_selector(BALANCE_OF_SELECTOR) + encode(["address"], [to_checksum_address(wallet)])).hex()
    raw = rpc.call("eth_call", [{"to": token, "data": data}, "latest"])
    return _int(raw)


def allowance(token, wallet, rpc):
    data = "0x" + (_selector("allowance(address,address)") +
                   encode(["address", "address"], [to_checksum_address(wallet), SWAP_ROUTER_02])).hex()
    raw = rpc.call("eth_call", [{"to": token, "data": data}, "latest"])
    return _int(raw)


def prepare_sell(token_in, amount_tokens, wallet, rpc, *, slippage_bps=DEFAULT_SLIPPAGE_BPS, fee=FEE):
    """Quote a token->ETH sell via V3 and build the router calldata. Read-only."""
    token_in, wallet = _addr(token_in), _addr(wallet)
    if slippage_bps < 0 or slippage_bps > MAX_SLIPPAGE_BPS:
        raise ValueError("slippage must be 0..5000 bps")
    if fee not in (100, 500, 3000, 10000):
        raise ValueError("unsupported fee tier")
    dec, symbol = token_meta(token_in, rpc)
    if amount_tokens == "all":
        amount_raw = token_balance_raw(token_in, wallet, rpc)
    else:
        try:
            amount = Decimal(str(amount_tokens))
        except (InvalidOperation, ValueError):
            raise ValueError("amount must be a number or 'all'")
        if not amount.is_finite() or amount <= 0:
            raise ValueError("amount must be positive")
        amount_raw = int(amount * 10 ** dec)
    if amount_raw <= 0:
        raise ValueError("no token balance to sell")

    quote_args = encode(["(address,address,uint256,uint24,uint160)"], [(token_in, WETH, amount_raw, fee, 0)])
    quote_data = "0x" + (_selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))") + quote_args).hex()
    try:
        raw = rpc.call("eth_call", [{"to": QUOTER_V2, "data": quote_data}, "latest"])
    except Exception as exc:
        raise RuntimeError("no quotable V3 pool to sell this token into ETH") from exc
    amount_out = int(decode(["uint256", "uint160", "uint32", "uint256"], bytes.fromhex(raw[2:]))[0])
    if amount_out <= 0:
        raise ValueError("quote returned zero ETH out")
    min_out = amount_out * (10000 - slippage_bps) // 10000

    swap_args = encode(["(address,address,uint24,address,uint256,uint256,uint160)"],
                       [(token_in, WETH, fee, wallet, amount_raw, min_out, 0)])
    swap_data = "0x" + (_selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))") + swap_args).hex()
    unwrap = encode(["uint256", "address"], [min_out, wallet])
    unwrap_data = "0x" + (_selector("unwrapWETH9(uint256,address)") + unwrap).hex()
    data = "0x" + (_selector("multicall(bytes[])") +
                   encode(["bytes[]"], [[bytes.fromhex(swap_data[2:]), bytes.fromhex(unwrap_data[2:])]])).hex()

    needs_approval = allowance(token_in, wallet, rpc) < amount_raw
    approve_data = "0x" + (_selector(APPROVE_SELECTOR) +
                           encode(["address", "uint256"], [SWAP_ROUTER_02, amount_raw])).hex()

    call = {"from": wallet, "to": SWAP_ROUTER_02, "data": data, "value": "0x0"}
    gas = _int(rpc.call("eth_estimateGas", [call, "pending"]))
    nonce = _int(rpc.call("eth_getTransactionCount", [wallet, "pending"]))
    try:
        gas_price = _int(rpc.call("eth_gasPrice", []))
    except Exception:
        gas_price = 0
    chain = _int(rpc.call("eth_chainId", []))
    if chain != CHAIN_ID:
        raise ValueError("RPC chain id mismatch")
    return {"kind": "evm_sell", "chain_id": chain, "wallet": wallet, "token_in": token_in,
            "symbol": symbol, "decimals": dec, "amount_in": amount_raw,
            "amount_out": amount_out, "min_out": min_out, "slippage_bps": slippage_bps,
            "fee": fee, "venue": "uniswap_v3", "needs_approval": needs_approval,
            "approve_data": approve_data, "nonce": nonce, "gas_estimate": gas,
            "gas": gas * 120 // 100, "gas_price": gas_price,
            "to": SWAP_ROUTER_02, "data": data, "value": 0}


def execute_sell(prepared, private_key, bcast):
    """Approve (if needed) then sign and broadcast the sell. Final confirm already given."""
    if not prepared or prepared.get("chain_id") != CHAIN_ID:
        return False, "wrong chain"
    try:
        acct = Account.from_key(private_key)
        if acct.address.lower() != prepared["wallet"].lower():
            return False, "wallet/key mismatch"

        def _tx(to, data, gas, nonce_v):
            tx = {"chainId": CHAIN_ID, "nonce": nonce_v, "to": to_checksum_address(to),
                  "data": data, "value": 0, "gas": gas}
            if int(prepared.get("gas_price") or 0):
                tx["gasPrice"] = int(prepared["gas_price"])
            else:
                tx["maxPriorityFeePerGas"] = 2_000_000_000
                tx["maxFeePerGas"] = 50_000_000_000
            signed = Account.sign_transaction(tx, private_key).raw_transaction.hex()
            out = bcast.broadcast("0x" + signed)
            txid = next((v for v in out.values() if isinstance(v, str) and v.startswith("0x") and len(v) == 66), None)
            return txid

        nonce = int(prepared["nonce"])
        if prepared.get("needs_approval"):
            approve_tx = _tx(prepared["token_in"], prepared["approve_data"], 80000, nonce)
            if not approve_tx:
                return False, "approval broadcast failed"
            nonce += 1
            deadline = time.time() + 45
            approved = False
            while time.time() < deadline:
                try:
                    rec = bcast.call("eth_getTransactionReceipt", [approve_tx])
                    if rec and rec.get("status") == "0x1":
                        approved = True
                        break
                    if rec and rec.get("status") == "0x0":
                        return False, "approval failed on chain"
                except Exception:
                    pass
                time.sleep(3)
            if not approved:
                return False, f"approval receipt not confirmed; swap was not sent (approval tx {approve_tx})"

        swap_tx = _tx(prepared["to"], prepared["data"], int(prepared["gas"]), nonce)
        if not swap_tx:
            return False, "RPC did not accept transaction"
        return True, swap_tx
    except Exception as exc:
        return False, f"transaction failed: {type(exc).__name__}"
