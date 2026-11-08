"""Guarded Uniswap Trading API swaps for supported EVM mainnets."""
from decimal import Decimal, InvalidOperation

import httpx

from acobot import evmswap
from acobot.tokenbuy import _addr, _int, _selector

BASE_URL = "https://trade-api.gateway.uniswap.org/v1"
NATIVE = "0x0000000000000000000000000000000000000000"
SLIPPAGE_PERCENT = 2.0
CHAINS = {
    "ethereum": {
        "chain_id": 1,
        "routers": {
            "0x66a9893cc07d91d95644aedd05d03f95e1dba8af",
            "0x23617e59a5925b2a4bf75d73ff6711cd0b29de85",
        },
    },
    "robinhood": {
        "chain_id": 4663,
        "routers": {"0x204faca1764b154221e35c0d20abb3c525710498"},
    },
    "ink": {
        "chain_id": 57073,
        "routers": {"0x661e93cca42afacb172121ef892830ca3b70f08d"},
    },
}


def _api_int(value):
    if isinstance(value, str):
        return int(value, 16) if value.startswith("0x") else int(value)
    return int(value)


def _api_post(path, payload, api_key):
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "x-permit2-disabled": "true",
        "x-universal-router-version": "2.1.2",
    }
    try:
        response = httpx.post(f"{BASE_URL}/{path}", json=payload, headers=headers, timeout=20)
    except httpx.RequestError as exc:
        raise RuntimeError("could not reach the Uniswap Trading API") from exc
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"Uniswap Trading API returned HTTP {response.status_code}") from exc
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Uniswap Trading API returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Uniswap Trading API returned an invalid response")
    return result


def _token_decimals(token, rpc):
    raw = rpc.call(
        "eth_call",
        [{"to": token, "data": "0x" + _selector("decimals()").hex()}, "latest"],
    )
    decimals = _int(raw)
    if not 0 <= decimals <= 36:
        raise ValueError("token has unsupported decimals")
    return decimals


def _token_symbol(token, rpc):
    raw = rpc.call(
        "eth_call",
        [{"to": token, "data": "0x" + _selector("symbol()").hex()}, "latest"],
    )
    body = bytes.fromhex(raw[2:])
    if len(body) == 32:
        return body.rstrip(b"\0").decode("utf-8", "ignore") or "TOKEN"
    if len(body) >= 64:
        size = int.from_bytes(body[:32], "big")
        return body[32:32 + size].decode("utf-8", "ignore") or "TOKEN"
    return "TOKEN"


def _amount_in(mode, token, amount, wallet, rpc):
    if mode == "buy":
        try:
            value = Decimal(str(amount))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("amount must be a number") from exc
        if not value.is_finite() or value <= 0:
            raise ValueError("amount must be positive")
        raw = int(value * 10**18)
        decimals = _token_decimals(token, rpc)
        symbol = _token_symbol(token, rpc)
    elif mode == "sell":
        decimals = _token_decimals(token, rpc)
        symbol = _token_symbol(token, rpc)
        if amount == "all":
            raw = evmswap.balance_raw(token, wallet, rpc)
        else:
            try:
                value = Decimal(str(amount))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("amount must be a number or 'all'") from exc
            if not value.is_finite() or value <= 0:
                raise ValueError("amount must be positive")
            raw = int(value * 10**decimals)
    else:
        raise ValueError("trade mode must be buy or sell")
    if raw <= 0:
        raise ValueError("trade amount is too small or token balance is zero")
    return raw, decimals, symbol


def prepare(chain_name, token, amount, wallet, rpc, *, mode="buy", api_key):
    """Fetch a Classic quote and transaction; never signs or broadcasts."""
    config = CHAINS.get(chain_name)
    if not config:
        raise ValueError(f"{chain_name} swaps are not supported by the Uniswap API")
    if not api_key or not api_key.strip():
        raise ValueError("set UNISWAP_API_KEY to use Uniswap API routing")
    token, wallet = _addr(token), _addr(wallet)
    chain_id = _int(rpc.call("eth_chainId", []))
    if chain_id != config["chain_id"]:
        raise ValueError("RPC chain id mismatch")

    amount_raw, decimals, symbol = _amount_in(mode, token, amount, wallet, rpc)
    token_in = NATIVE if mode == "buy" else token
    token_out = token if mode == "buy" else NATIVE
    request = {
        "type": "EXACT_INPUT",
        "amount": str(amount_raw),
        "tokenInChainId": chain_id,
        "tokenOutChainId": chain_id,
        "tokenIn": token_in,
        "tokenOut": token_out,
        "swapper": wallet,
        "recipient": wallet,
        "slippageTolerance": SLIPPAGE_PERCENT,
        "routingPreference": "BEST_PRICE",
        "protocols": ["V2", "V3", "V4"],
    }
    response = _api_post("quote", request, api_key.strip())
    if response.get("routing") != "CLASSIC":
        raise ValueError("Uniswap returned a non-Classic route; this bot only supports Classic swaps")
    quote = response.get("quote")
    if not isinstance(quote, dict):
        raise ValueError("Uniswap API did not return a quote")
    if _api_int(quote.get("chainId", 0)) != chain_id:
        raise ValueError("Uniswap quote chain id mismatch")
    if str(quote.get("swapper", "")).lower() != wallet.lower():
        raise ValueError("Uniswap quote wallet mismatch")
    quoted_input = quote.get("input") or {}
    quoted_output = quote.get("output") or {}
    if (
        str(quoted_input.get("token", "")).lower() != token_in.lower()
        or str(quoted_output.get("token", "")).lower() != token_out.lower()
        or _api_int(quoted_input.get("amount", 0)) != amount_raw
    ):
        raise ValueError("Uniswap quote does not match the requested token and amount")
    if str(quoted_output.get("recipient", "")).lower() != wallet.lower():
        raise ValueError("Uniswap quote output recipient does not match the wallet")
    amount_out = _api_int(quoted_output.get("amount", 0))
    aggregate = quote.get("aggregatedOutputs") or []
    if not aggregate or any(
        not isinstance(item, dict)
        or str(item.get("token", "")).lower() != token_out.lower()
        or str(item.get("recipient", "")).lower() != wallet.lower()
        or item.get("fee") is not None
        for item in aggregate
    ):
        raise ValueError("Uniswap quote returned an unexpected output token or recipient")
    min_out = sum(_api_int(item.get("minAmount", 0)) for item in aggregate if isinstance(item, dict))
    if amount_out <= 0 or min_out <= 0:
        raise ValueError("Uniswap quote returned an invalid output amount")

    swap_response = _api_post(
        "swap",
        {
            "quote": quote,
            "simulateTransaction": True,
            "refreshGasPrice": True,
            "safetyMode": "SAFE",
        },
        api_key.strip(),
    )
    tx = swap_response.get("swap")
    if not isinstance(tx, dict):
        raise ValueError("Uniswap API did not return swap transaction data")
    if _api_int(tx.get("chainId", 0)) != chain_id:
        raise ValueError("Uniswap transaction chain id mismatch")
    if str(tx.get("from", "")).lower() != wallet.lower():
        raise ValueError("Uniswap transaction wallet mismatch")
    to = _addr(tx.get("to", ""))
    if to.lower() not in config["routers"]:
        raise ValueError("Uniswap returned an unverified router address")
    data = tx.get("data")
    if not isinstance(data, str) or not data.startswith("0x") or len(data) < 10:
        raise ValueError("Uniswap API returned invalid transaction calldata")
    api_gas_limit = _api_int(tx.get("gasLimit", 0))
    if api_gas_limit < 0 or api_gas_limit > 30_000_000:
        raise ValueError("Uniswap API returned an invalid gas limit")
    value = _api_int(tx.get("value", 0))
    expected_value = amount_raw if mode == "buy" else 0
    if value != expected_value:
        raise ValueError("Uniswap transaction value does not match the requested trade")
    max_fee_per_gas = _api_int(tx.get("maxFeePerGas", 0))
    max_priority_fee_per_gas = _api_int(tx.get("maxPriorityFeePerGas", 0))
    if max_fee_per_gas and (
        not max_priority_fee_per_gas or max_priority_fee_per_gas > max_fee_per_gas
    ):
        raise ValueError("Uniswap API returned invalid EIP-1559 fee values")
    call = {"from": wallet, "to": to, "data": data, "value": hex(value)}
    rpc_gas_limit = _int(rpc.call("eth_estimateGas", [call, "pending"]))
    gas_limit = max(api_gas_limit, rpc_gas_limit)
    if gas_limit <= 0 or gas_limit > 30_000_000:
        raise ValueError("could not estimate a valid Uniswap transaction gas limit")

    needs_approval = mode == "sell" and evmswap.allowance(token, wallet, to, rpc) < amount_raw
    nonce = _int(rpc.call("eth_getTransactionCount", [wallet, "pending"]))
    prepared = {
        "kind": f"{chain_name}_{mode}",
        "chain_name": chain_name,
        "chain_id": chain_id,
        "wallet": wallet,
        "token": token,
        "symbol": symbol,
        "decimals": decimals,
        "amount_in": amount_raw,
        "amount_out": amount_out,
        "min_out": min_out,
        "slippage_bps": int(SLIPPAGE_PERCENT * 100),
        "venue": "uniswap_trading_api_classic",
        "mode": mode,
        "needs_approval": needs_approval,
        "approve_data": evmswap.approve_data(to, amount_raw),
        "nonce": nonce,
        "gas_estimate": gas_limit,
        "gas": gas_limit * 120 // 100,
        "gas_price": _api_int(tx.get("gasPrice", 0)),
        "max_fee_per_gas": max_fee_per_gas,
        "max_priority_fee_per_gas": max_priority_fee_per_gas,
        "to": to,
        "data": data,
        "value": value,
    }
    if not prepared["gas_price"] and not prepared["max_fee_per_gas"]:
        prepared["gas_price"] = _int(rpc.call("eth_gasPrice", []))
    return prepared
