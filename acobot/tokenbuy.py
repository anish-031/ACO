"""Guarded native-ETH token buys on Robinhood Chain.

Uniswap V4 is preferred, with V3 as fallback. Preparation is read-only; only
execute_prepared signs and broadcasts after the Telegram final-confirm callback.
"""
from decimal import Decimal
import time
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

CHAIN_ID = 4663
QUOTER_V2 = "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"
SWAP_ROUTER_02 = "0xcaf681a66d020601342297493863e78c959e5cb2"
LIQUIDSWAP_API = "https://api.liqd.ag/v2/route"
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"
V4_ROUTER = "0x8876789976decbfcbbbe364623c63652db8c0904"
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
STATE_VIEW = "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ROUTER_SELF = "0x0000000000000000000000000000000000000002"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
# Exact PoolKey emitted by the live Initialize event for the target Robinhood pool.
V4_FEE = 8388608
V4_TICK_SPACING = 60
V4_HOOK = "0x7a65d0194e6cc43971c31ce7d1471da01d42a0cc"
FEE = 3000
DEFAULT_SLIPPAGE_BPS = 200
MAX_SLIPPAGE_BPS = 5000
V4_QUOTE_UNAVAILABLE = "v4 quote reverted (0x); hook-dependent execution cannot be safely quoted"


def _selector(signature): return keccak(text=signature)[:4]


def _addr(address):
    if not isinstance(address, str) or len(address) != 42 or not address.startswith("0x"):
        raise ValueError("invalid address")
    return to_checksum_address(address)


def _int(v): return int(v, 16) if isinstance(v, str) else int(v)


def _prepare_v3(token_out, wallet, amount_in, slippage_bps, rpc, fee):
    quote_args = encode(["(address,address,uint256,uint24,uint160)"], [(WETH, token_out, amount_in, fee, 0)])
    quote_data = "0x" + (_selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))") + quote_args).hex()
    try:
        quote_raw = rpc.call("eth_call", [{"to": QUOTER_V2, "data": quote_data}, "latest"])
    except Exception as exc:
        raise RuntimeError("v3 quote unavailable") from exc
    amount_out = int(decode(["uint256", "uint160", "uint32", "uint256"], bytes.fromhex(quote_raw[2:]))[0])
    if amount_out <= 0: raise ValueError("no quoted output")
    min_out = amount_out * (10000-slippage_bps)//10000
    swap_args = encode(["(address,address,uint24,address,uint256,uint256,uint160)"],
                       [(WETH, token_out, fee, wallet, amount_in, min_out, 0)])
    data = "0x" + (_selector("exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))") + swap_args).hex()
    return {"venue":"uniswap_v3", "amount_out":amount_out, "min_out":min_out,
            "to":SWAP_ROUTER_02, "data":data, "value":amount_in, "fee":fee}


def _v4_pool_key(token_out, rpc):
    """Use the exact initialized WETH/token key, including the hook and dynamic-fee flag."""
    token_out = _addr(token_out)
    currency0, currency1 = sorted((WETH, token_out), key=lambda value: int(value, 16))
    key = (to_checksum_address(currency0), to_checksum_address(currency1),
           V4_FEE, V4_TICK_SPACING, to_checksum_address(V4_HOOK))
    pool_id = "0x" + keccak(encode(["address", "address", "uint24", "int24", "address"], key)).hex()
    slot0_data = "0x" + (_selector("getSlot0(bytes32)") + bytes.fromhex(pool_id[2:])).hex()
    slot0_raw = rpc.call("eth_call", [{"to": STATE_VIEW, "data": slot0_data}, "latest"])
    slot0 = decode(["uint160", "int24", "uint24", "uint24"], bytes.fromhex(slot0_raw[2:]))
    if int(slot0[0]) == 0:
        raise ValueError("v4 pool is not initialized")
    return pool_id, key


def _prepare_v4(token_out, wallet, amount_in, slippage_bps, rpc):
    pool_id, key = _v4_pool_key(token_out, rpc)
    zero_for_one = key[0].lower() == WETH.lower()
    keytype = "(address,address,uint24,int24,address)"
    args = encode([keytype, "bool", "uint128", "bytes"], [key, zero_for_one, amount_in, b""])
    quote_signature = "quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))"
    quote_data = "0x" + (_selector(quote_signature) + args).hex()
    try:
        raw = rpc.call("eth_call", [{"to": V4_QUOTER, "data": quote_data}, "latest"])
    except Exception as exc:
        raise RuntimeError(V4_QUOTE_UNAVAILABLE) from exc
    try:
        amount_out = int(decode(["uint256", "uint256"], bytes.fromhex(raw[2:]))[0])
    except Exception as exc:
        raise RuntimeError("could not decode v4 quote") from exc
    if amount_out <= 0:
        raise ValueError("v4 quote returned zero")
    min_out = amount_out * (10000 - slippage_bps) // 10000

    # Universal Router v2: wrap supplied native ETH then execute exact-input V4 swap.
    wrapped_eth = encode(["address", "uint256"], [ROUTER_SELF, amount_in])
    actions = bytes([0x06, 0x0c, 0x0f])
    swap_param_type = "((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"
    swap_params = encode([swap_param_type], [(key, zero_for_one, amount_in, min_out, b"")])
    settle_params = encode(["address", "uint256"], [WETH, amount_in])
    output_currency = key[1] if zero_for_one else key[0]
    take_params = encode(["address", "address", "uint256"], [output_currency, wallet, 0])
    v4_input = encode(["bytes", "bytes[]"], [actions, [swap_params, settle_params, take_params]])
    commands = bytes([0x0b, 0x10])
    deadline = int(time.time()) + 120
    router_args = encode(["bytes", "bytes[]", "uint256"], [commands, [wrapped_eth, v4_input], deadline])
    data = "0x" + (_selector("execute(bytes,bytes[],uint256)") + router_args).hex()
    rpc.call("eth_call", [{"from": wallet, "to": V4_ROUTER, "data": data, "value": hex(amount_in)}, "latest"])
    return {"venue": "uniswap_v4", "pool_id": pool_id, "amount_out": amount_out,
            "min_out": min_out, "to": V4_ROUTER, "data": data, "value": amount_in, "fee": V4_FEE}


def prepare_buy(token_out, amount_eth, wallet, rpc, *, slippage_bps=DEFAULT_SLIPPAGE_BPS, fee=FEE):
    """Read quote and estimate gas; never signs or broadcasts."""
    token_out, wallet = _addr(token_out), _addr(wallet)
    amount = Decimal(str(amount_eth))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be positive")
    if not isinstance(slippage_bps, int) or not 0 <= slippage_bps <= MAX_SLIPPAGE_BPS:
        raise ValueError("slippage must be 0..5000 bps")
    if fee not in (100, 500, 3000, 10000):
        raise ValueError("unsupported fee tier")
    amount_in = int(amount * 10**18)
    if amount_in <= 0:
        raise ValueError("amount too small")

    # Prefer the known v4 native-ETH path and use V3 only if no usable V4 path exists.
    try:
        quoted = _prepare_v4(token_out, wallet, amount_in, slippage_bps, rpc)
    except Exception as v4_error:
        last_v3 = None
        for tier in (fee, 10000, 500, 100):
            try:
                quoted = _prepare_v3(token_out, wallet, amount_in, slippage_bps, rpc, tier)
                break
            except Exception as tier_error:
                last_v3 = tier_error
        else:
            if "v4 quote reverted" in str(v4_error).lower():
                raise RuntimeError(f"{V4_QUOTE_UNAVAILABLE}; v3 fallback also unavailable") from last_v3
            raise ValueError("no quotable pool found for this token on robinhood chain") from last_v3
    call = {"from":wallet,"to":quoted["to"],"data":quoted["data"],"value":hex(quoted["value"])}
    gas = _int(rpc.call("eth_estimateGas", [call, "pending"]))
    nonce = _int(rpc.call("eth_getTransactionCount", [wallet, "pending"]))
    try:
        gas_price = _int(rpc.call("eth_gasPrice", []))
    except Exception:
        gas_price = 0
    chain = _int(rpc.call("eth_chainId", []))
    if chain != CHAIN_ID:
        raise ValueError("RPC chain id mismatch")
    return {"chain_id":chain,"wallet":wallet,"token_out":token_out,"amount_in":amount_in,
            "amount_out":quoted["amount_out"],"min_out":quoted["min_out"],"slippage_bps":slippage_bps,
            "fee":quoted["fee"],"venue":quoted["venue"],"pool_id":quoted.get("pool_id"),
            "route":quoted.get("route",[]),"nonce":nonce,"gas_estimate":gas,"gas":gas*120//100,
            "gas_price":gas_price,"to":quoted["to"],"data":quoted["data"],"value":quoted["value"]}


def execute_prepared(prepared, private_key, bcast):
    """Call only after the final explicit Telegram confirmation tap."""
    if not prepared or prepared.get("chain_id") != CHAIN_ID: return False,"wrong chain"
    try:
        acct=Account.from_key(private_key)
        if acct.address.lower()!=prepared["wallet"].lower(): return False,"wallet/key mismatch"
        tx={"chainId":CHAIN_ID,"nonce":int(prepared["nonce"]),"to":to_checksum_address(prepared["to"]),
            "data":prepared["data"],"value":int(prepared["value"]),"gas":int(prepared["gas"])}
        if int(prepared.get("gas_price") or 0): tx["gasPrice"]=int(prepared["gas_price"])
        else: tx["maxPriorityFeePerGas"]=2_000_000_000; tx["maxFeePerGas"]=50_000_000_000
        signed=Account.sign_transaction(tx,private_key).raw_transaction.hex()
        out=bcast.broadcast("0x"+signed)
        txid=next((v for v in out.values() if isinstance(v,str) and v.startswith("0x") and len(v)==66),None)
        return (True,txid) if txid else (False,"RPC did not accept transaction")
    except Exception as exc: return False,f"transaction failed: {type(exc).__name__}"


def format_amount(raw, decimals=18, places=8):
    return f"{Decimal(int(raw))/(Decimal(10)**decimals):,.{places}f}".rstrip("0").rstrip(".")
