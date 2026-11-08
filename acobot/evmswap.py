"""Generic Uniswap swaps for ethereum (V3) and ink (V4), reusing the guarded
prepare/confirm/execute pattern from tokenbuy. Quotes are read-only; execution
happens only after the Telegram final-confirm tap. Addresses verified against
Uniswap's official deployment docs and on-chain code checks (2026-10-30)."""
import time
from decimal import Decimal, InvalidOperation
from eth_abi import encode, decode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from acobot.tokenbuy import (_selector, _addr, _int, format_amount)

CHAINS = {
    "ethereum": {
        "chain_id": 1, "venue": "uniswap_v3",
        "quoter": "0x61fFE014bA17989E743c5F6cB21bF9697530B21e",
        "router": "0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",
        "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "native": "ETH",
    },
    "ink": {
        "chain_id": 57073, "venue": "uniswap_v4",
        "quoter": "0x8928074CA1b241D8Ec02815881c1Af11E8bC5219",
        "state_view": "0x76fd297e2d437cd7f76d50f01afe6160f86e9990",
        "pool_manager": "0x360e68faccca8ca495c1b759fd9eee466db9fb32",
        "router": "0x661E93cca42AfacB172121EF892830cA3b70F08d",
        "weth": "0x4200000000000000000000000000000000000006",
        "native": "ETH",
        # (fee, tick_spacing, hook) candidates tried in order until one quotes
        "pool_candidates": [(500, 10, "0x" + "0" * 40), (3000, 60, "0x" + "0" * 40),
                            (10000, 200, "0x" + "0" * 40),
                            (8388608, 60, "0x7a65d0194e6cc43971c31ce7d1471da01d42a0cc")],
    },
}

ERC20 = {"balance_of": "balanceOf(address)", "decimals": "decimals()",
         "allowance": "allowance(address,address)", "approve": "approve(address,uint256)", "symbol": "symbol()"}


def _read_string(body):
    if len(body) >= 64:
        length = int.from_bytes(body[0:32], "big")
        return body[32:32 + length].decode("utf-8", "ignore")
    return ""


def token_meta(token, rpc):
    dec = 18
    try:
        raw = rpc.call("eth_call", [{"to": token, "data": "0x" + _selector(ERC20["decimals"]).hex()}, "latest"])
        dec = _int(raw)
    except Exception:
        pass
    if not 0 < dec <= 36:
        dec = 18
    symbol = "TOKEN"
    try:
        raw = rpc.call("eth_call", [{"to": token, "data": "0x" + _selector(ERC20["symbol"]).hex()}, "latest"])
        symbol = _read_string(bytes.fromhex(raw[2:])) or "TOKEN"
    except Exception:
        pass
    return dec, symbol


def balance_raw(token, wallet, rpc):
    data = "0x" + (_selector(ERC20["balance_of"]) + encode(["address"], [to_checksum_address(wallet)])).hex()
    return _int(rpc.call("eth_call", [{"to": token, "data": data}, "latest"]))


def allowance(token, wallet, spender, rpc):
    data = "0x" + (_selector(ERC20["allowance"]) +
                   encode(["address", "address"], [to_checksum_address(wallet), spender])).hex()
    return _int(rpc.call("eth_call", [{"to": token, "data": data}, "latest"]))


def approve_data(spender, amount_raw):
    return "0x" + (_selector(ERC20["approve"]) +
                   encode(["address", "uint256"], [spender, amount_raw])).hex()


def _v3_quote(cfg, token, amount_raw, wallet, rpc, slippage_bps, sell):
    weth = cfg["weth"]
    pair = (token, weth) if sell else (weth, token)
    quote_args = encode(["(address,address,uint256,uint24,uint160)"], [(pair[0], pair[1], amount_raw, 3000, 0)])
    quote_data = "0x" + (_selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))") + quote_args).hex()
    raw = rpc.call("eth_call", [{"to": cfg["quoter"], "data": quote_data}, "latest"])
    amount_out = int(decode(["uint256", "uint160", "uint32", "uint256"], bytes.fromhex(raw[2:]))[0])
    if amount_out <= 0:
        raise ValueError("quote returned zero")
    min_out = amount_out * (10000 - slippage_bps) // 10000
    if sell:
        swap_args = encode(["(address,address,uint24,address,uint256,uint256,uint160)"],
                           [(token, weth, 3000, wallet, amount_raw, min_out, 0)])
        swap = "0x" + (_selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))") + swap_args).hex()
        unwrap = encode(["uint256", "address"], [min_out, wallet])
        unwrap_call = "0x" + (_selector("unwrapWETH9(uint256,address)") + unwrap).hex()
        data = "0x" + (_selector("multicall(bytes[])") +
                       encode(["bytes[]"], [[bytes.fromhex(swap[2:]), bytes.fromhex(unwrap_call[2:])]])).hex()
    else:
        swap_args = encode(["(address,address,uint24,address,uint256,uint256,uint160)"],
                           [(weth, token, 3000, wallet, amount_raw, min_out, 0)])
        data = "0x" + (_selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))") + swap_args).hex()
    return {"amount_out": amount_out, "min_out": min_out, "to": cfg["router"], "data": data,
            "value": 0 if sell else amount_raw, "venue": "uniswap_v3", "extra": {}}


def _v4_quote(cfg, token, amount_raw, wallet, rpc, slippage_bps, sell):
    weth = cfg["weth"]
    last = None
    for fee, spacing, hook in cfg["pool_candidates"]:
        key = (to_checksum_address(weth), to_checksum_address(token), fee, spacing, to_checksum_address(hook))
        pool_id = "0x" + keccak(encode(["address", "address", "uint24", "int24", "address"], key)).hex()
        slot0_data = "0x" + (_selector("getSlot0(bytes32)") + bytes.fromhex(pool_id[2:])).hex()
        try:
            slot0_raw = rpc.call("eth_call", [{"to": cfg["state_view"], "data": slot0_data}, "latest"])
            slot0 = decode(["uint160", "int24", "uint24", "uint24"], bytes.fromhex(slot0_raw[2:]))
        except Exception as exc:
            last = exc
            continue
        if int(slot0[0]) == 0:
            continue
        zero_for_one = key[0].lower() == weth.lower()  # ETH in -> token out
        keytype = "(address,address,uint24,int24,address)"
        args = encode([keytype, "bool", "uint128", "bytes"], [key, zero_for_one, amount_raw, b""])
        quote_sig = "quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))"
        quote_data = "0x" + (_selector(quote_sig) + args).hex()
        raw = rpc.call("eth_call", [{"to": cfg["quoter"], "data": quote_data}, "latest"])
        try:
            amount_out = int(decode(["uint256", "uint256"], bytes.fromhex(raw[2:]))[0])
        except Exception as exc:
            last = exc
            continue
        if amount_out <= 0:
            continue
        min_out = amount_out * (10000 - slippage_bps) // 10000
        # buy: wrap native ETH then v4 swap (universal router). sell: unsupported on v4 here.
        if sell:
            raise ValueError("v4 sell needs a token-input route; sell on ink is not supported yet")
        wrapped = encode(["address", "uint256"], ["0x0000000000000000000000000000000000000002", amount_raw])
        actions = bytes([0x06, 0x0c, 0x0f])
        swap_type = "((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"
        swap_params = encode([swap_type], [(key, zero_for_one, amount_raw, min_out, b"")])
        settle = encode(["address", "uint256"], [weth, amount_raw])
        out_currency = key[1] if zero_for_one else key[0]
        take = encode(["address", "address", "uint256"], [out_currency, wallet, 0])
        v4_input = encode(["bytes", "bytes[]"], [actions, [swap_params, settle, take]])
        commands = bytes([0x0b, 0x10])
        deadline = int(time.time()) + 120
        router_args = encode(["bytes", "bytes[]", "uint256"], [commands, [wrapped, v4_input], deadline])
        data = "0x" + (_selector("execute(bytes,bytes[],uint256)") + router_args).hex()
        return {"amount_out": amount_out, "min_out": min_out, "to": cfg["router"], "data": data,
                "value": amount_raw, "venue": "uniswap_v4",
                "extra": {"pool_fee": fee, "hook": hook}}
    raise ValueError("no initialized v4 pool found for this token on ink") if last is None else last


def _amount_raw(token, amount, wallet, rpc):
    if amount == "all":
        raw = balance_raw(token, wallet, rpc)
    else:
        try:
            value = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            raise ValueError("amount must be a number or 'all'")
        if not value.is_finite() or value <= 0:
            raise ValueError("amount must be positive")
        dec, _ = token_meta(token, rpc)
        raw = int(value * 10 ** dec)
    if raw <= 0:
        raise ValueError("no balance to trade")
    return raw


def prepare(chain_name, token, amount, wallet, rpc, *, mode="buy", slippage_bps=200):
    if chain_name == "robinhood_testnet":
        raise ValueError("Robinhood testnet swaps are disabled until its router, quoter, and token deployments are verified")
    cfg = CHAINS.get(chain_name)
    if not cfg:
        raise ValueError(f"{chain_name} is not supported for token trades")
    token, wallet = _addr(token), _addr(wallet)
    chain_id = _int(rpc.call("eth_chainId", []))
    if chain_id != cfg["chain_id"]:
        raise ValueError("RPC chain id mismatch")
    native_dec = 18
    if mode == "buy":
        try:
            value = Decimal(str(amount))
        except (InvalidOperation, ValueError):
            raise ValueError("amount must be a number")
        if not value.is_finite() or value <= 0:
            raise ValueError("amount must be positive")
        amount_raw = int(value * 10 ** native_dec)
    else:
        amount_raw = _amount_raw(token, amount, wallet, rpc)
    if cfg["venue"] == "uniswap_v3":
        quoted = _v3_quote(cfg, token, amount_raw, wallet, rpc, slippage_bps, sell=(mode == "sell"))
    else:
        quoted = _v4_quote(cfg, token, amount_raw, wallet, rpc, slippage_bps, sell=(mode == "sell"))
    needs_approval = False
    if mode == "sell":
        needs_approval = allowance(token, wallet, quoted["to"], rpc) < amount_raw
    call = {"from": wallet, "to": quoted["to"], "data": quoted["data"], "value": hex(quoted["value"])}
    gas = _int(rpc.call("eth_estimateGas", [call, "pending"]))
    nonce = _int(rpc.call("eth_getTransactionCount", [wallet, "pending"]))
    try:
        gas_price = _int(rpc.call("eth_gasPrice", []))
    except Exception:
        gas_price = 0
    prepared = {"kind": f"{chain_name}_{mode}", "chain_name": chain_name, "chain_id": chain_id,
                "wallet": wallet, "token": token, "symbol": "", "amount_in": amount_raw,
                "amount_out": quoted["amount_out"], "min_out": quoted["min_out"],
                "slippage_bps": slippage_bps, "venue": quoted["venue"], "mode": mode,
                "needs_approval": needs_approval, "approve_data": approve_data(quoted["to"], amount_raw),
                "nonce": nonce, "gas_estimate": gas, "gas": gas * 120 // 100, "gas_price": gas_price,
                "to": quoted["to"], "data": quoted["data"], "value": quoted["value"]}
    dec, symbol = token_meta(token, rpc)
    prepared["symbol"] = symbol
    prepared["decimals"] = dec
    return prepared


def execute_prepared(prepared, private_key, bcast):
    if not prepared or int(prepared.get("chain_id") or 0) <= 0:
        return False, "bad prepared swap"
    try:
        acct = Account.from_key(private_key)
        if acct.address.lower() != prepared["wallet"].lower():
            return False, "wallet/key mismatch"

        def _tx(to, data, value, gas, nonce_v):
            tx = {"chainId": int(prepared["chain_id"]), "nonce": nonce_v, "to": to_checksum_address(to),
                  "data": data, "value": int(value), "gas": int(gas)}
            if int(prepared.get("max_fee_per_gas") or 0):
                tx["maxPriorityFeePerGas"] = int(prepared["max_priority_fee_per_gas"])
                tx["maxFeePerGas"] = int(prepared["max_fee_per_gas"])
            elif int(prepared.get("gas_price") or 0):
                tx["gasPrice"] = int(prepared["gas_price"])
            else:
                tx["maxPriorityFeePerGas"] = 2_000_000_000
                tx["maxFeePerGas"] = 50_000_000_000
            signed = Account.sign_transaction(tx, private_key).raw_transaction.hex()
            out = bcast.broadcast("0x" + signed)
            return next((v for v in out.values() if isinstance(v, str) and v.startswith("0x") and len(v) == 66), None)

        nonce = int(prepared["nonce"])
        if prepared.get("needs_approval"):
            approve_tx = _tx(prepared["token"], prepared["approve_data"], 0, 80000, nonce)
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
        swap_tx = _tx(prepared["to"], prepared["data"], prepared["value"], int(prepared["gas"]), nonce)
        if not swap_tx:
            return False, "RPC did not accept transaction"
        return True, swap_tx
    except Exception as exc:
        return False, f"transaction failed: {type(exc).__name__}"
