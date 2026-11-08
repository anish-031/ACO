"""Jupiter swap API helpers. Builds unsigned transaction payloads only."""
import json
import urllib.parse
import urllib.request

QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
SWAP_URL = "https://api.jup.ag/swap/v1/swap"


def quote(input_mint, output_mint, amount, *, slippage_bps=200, api_key=None, opener=None):
    if not input_mint or not output_mint or int(amount) <= 0:
        raise ValueError("input/output mint and positive raw amount required")
    if not 0 <= int(slippage_bps) <= 5000:
        raise ValueError("slippage must be between 0 and 5000 bps")
    params = urllib.parse.urlencode({"inputMint": input_mint, "outputMint": output_mint,
                                     "amount": str(int(amount)), "slippageBps": str(int(slippage_bps)),
                                     "restrictIntermediateTokens": "true", "instructionVersion": "V2"})
    headers = {"Accept": "application/json", "User-Agent": "acobot/1.0"}
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(QUOTE_URL + "?" + params, headers=headers)
    with (opener or urllib.request.urlopen)(req, timeout=12) as response:
        data = json.loads(response.read())
    if not data.get("outAmount") or not data.get("otherAmountThreshold") or not data.get("routePlan"):
        raise RuntimeError("Jupiter returned no executable quote")
    return data


def build_swap(quote_response, user_public_key, *, api_key=None, opener=None):
    """Ask Jupiter for a base64 unsigned transaction. Signing/broadcast belongs to a confirmed wallet flow."""
    if not quote_response or not quote_response.get("routePlan") or not user_public_key:
        raise ValueError("valid Jupiter quote and wallet public key required")
    body = {"quoteResponse": quote_response, "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True}
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "acobot/1.0"}
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(SWAP_URL, data=json.dumps(body).encode(), headers=headers, method="POST")
    with (opener or urllib.request.urlopen)(req, timeout=20) as response:
        data = json.loads(response.read())
    if not data.get("swapTransaction"):
        raise RuntimeError("Jupiter returned no swap transaction")
    return data
