"""Solana wallet primitives, read-only balance lookup, and confirmed Jupiter swap execution."""
import base64
import json
import zlib
from decimal import Decimal, InvalidOperation
import threading
import time

from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TokenAccountOpts, TxOpts
from solders.pubkey import Pubkey

RPC_URL = "https://api.mainnet-beta.solana.com"
MAINNET_GENESIS_HASH = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"
_rpc_url = RPC_URL
_rpc_urls = [RPC_URL]
_clients = {}
_clients_lock = threading.Lock()
_verified_urls = set()
_verification_lock = threading.Lock()
WSOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAM_IDS = (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID)
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
_SUPPORTED_TOKEN_2022_MINT_EXTENSIONS = {
    "mintcloseauthority", "metadatapointer", "tokenmetadata",
    "grouppointer", "group", "groupmemberpointer", "groupmember",
    "permanentdelegate",
}


def configure_rpc_url(url):
    """Set the dedicated Solana mainnet endpoint loaded from bot configuration."""
    configure_rpc_urls([url])


def configure_rpc_urls(urls):
    """Set a primary Solana mainnet RPC and ordered failover endpoints."""
    global _rpc_url, _rpc_urls
    if isinstance(urls, str):
        urls = [urls]
    values = list(dict.fromkeys(str(url or "").strip() for url in urls or []))
    if not values or any(not value.startswith(("https://", "http://")) for value in values):
        raise ValueError("Solana RPC URLs must contain at least one HTTP or HTTPS endpoint")
    _rpc_urls = values
    _rpc_url = values[0]
    with _verification_lock:
        _verified_urls.clear()


def _client(rpc_url):
    with _clients_lock:
        client = _clients.get(rpc_url)
        if client is None:
            client = Client(rpc_url, commitment=Confirmed, timeout=12)
            _clients[rpc_url] = client
        return client


def _response_result(response):
    body = json.loads(response.to_json())
    if "error" in body:
        raise RuntimeError(f"solana RPC failed: {body['error']}")
    if "result" not in body:
        raise RuntimeError("solana RPC returned no result")
    return body["result"]


def _call_client(client, method, values):
    if method == "getGenesisHash":
        return _response_result(client.get_genesis_hash())
    if method == "getBalance":
        return _response_result(client.get_balance(
            Pubkey.from_string(values[0]), commitment=Confirmed))
    if method == "getTokenAccountsByOwner":
        options = values[1]
        opts = TokenAccountOpts(mint=Pubkey.from_string(options["mint"]), encoding="jsonParsed")
        return _response_result(client.get_token_accounts_by_owner_json_parsed(
            Pubkey.from_string(values[0]), opts, commitment=Confirmed))
    if method == "getTokenSupply":
        return _response_result(client.get_token_supply(
            Pubkey.from_string(values[0]), commitment=Confirmed))
    if method == "getAccountInfo":
        account = Pubkey.from_string(values[0])
        options = values[1] if len(values) > 1 else {}
        if options.get("encoding") == "base64":
            return _response_result(client.get_account_info(
                account, commitment=Confirmed, encoding="base64"))
        return _response_result(client.get_account_info_json_parsed(
            account, commitment=Confirmed))
    if method == "getLatestBlockhash":
        return _response_result(client.get_latest_blockhash(commitment=Confirmed))
    if method == "getSignatureStatuses":
        from solders.signature import Signature
        signatures = [Signature.from_string(value) for value in values[0]]
        options = values[1] if len(values) > 1 else {}
        return _response_result(client.get_signature_statuses(
            signatures, search_transaction_history=bool(options.get("searchTransactionHistory", False))))
    if method == "getMinimumBalanceForRentExemption":
        return _response_result(client.get_minimum_balance_for_rent_exemption(
            int(values[0]), commitment=Confirmed))
    if method == "sendTransaction":
        opts = values[1] if len(values) > 1 else {}
        raw = base64.b64decode(values[0], validate=True)
        tx_opts = TxOpts(skip_preflight=bool(opts.get("skipPreflight", False)),
                         preflight_commitment=opts.get("preflightCommitment", Confirmed))
        return _response_result(client.send_raw_transaction(raw, opts=tx_opts))
    raise ValueError(f"unsupported Solana RPC method: {method}")


def _verify_mainnet(rpc_url):
    with _verification_lock:
        if rpc_url in _verified_urls:
            return
        genesis = _response_result(_client(rpc_url).get_genesis_hash())
        if genesis != MAINNET_GENESIS_HASH:
            raise RuntimeError("Solana RPC endpoint is not on Solana mainnet")
        _verified_urls.add(rpc_url)


def _safe_rpc_error(exc, endpoint):
    detail = str(exc).replace(endpoint, "<rpc endpoint>").strip()
    return f"{type(exc).__name__}: {detail[:120]}" if detail else type(exc).__name__


def rpc_call(method, params=None, *, rpc_url=None):
    values = params or []
    if method not in {
        "getGenesisHash", "getBalance", "getTokenAccountsByOwner", "getTokenSupply",
        "getAccountInfo", "getLatestBlockhash", "getSignatureStatuses",
        "getMinimumBalanceForRentExemption", "sendTransaction",
    }:
        raise ValueError(f"unsupported Solana RPC method: {method}")
    urls = [rpc_url] if rpc_url else list(_rpc_urls)
    failures = []
    for index, endpoint in enumerate(urls, 1):
        try:
            _verify_mainnet(endpoint)
            return _call_client(_client(endpoint), method, values)
        except Exception as exc:
            failures.append(f"endpoint {index}: {_safe_rpc_error(exc, endpoint)}")
    detail = "; ".join(failures) if failures else "no endpoints configured"
    raise RuntimeError(
        f"no verified Solana mainnet RPC endpoint succeeded for {method} ({detail})")


class ProgramInspectionError(ValueError):
    pass


def inspect_program(program_id, *, rpc_url=None, call=rpc_call):
    """Inspect an executable Solana program and its legacy on-chain Anchor IDL, read-only."""
    try:
        program = Pubkey.from_string(str(program_id))
    except Exception as exc:
        raise ValueError("invalid Solana program address") from exc

    program_info = call(
        "getAccountInfo", [str(program), {"encoding": "base64"}], rpc_url=rpc_url)
    program_account = program_info.get("value")
    if not program_account:
        raise ProgramInspectionError("program account was not found on Solana mainnet")
    if not program_account.get("executable"):
        raise ProgramInspectionError("address is not an executable Solana program")

    signer = Pubkey.find_program_address([], program)[0]
    idl_address = Pubkey.create_with_seed(signer, "anchor:idl", program)
    idl_info = call(
        "getAccountInfo", [str(idl_address), {"encoding": "base64"}], rpc_url=rpc_url)
    account = idl_info.get("value")
    if not account:
        raise ProgramInspectionError(
            "no published on-chain Anchor IDL found; no mint instruction was inferred")
    if account.get("owner") != str(program):
        raise ProgramInspectionError("IDL account owner does not match the selected program")

    encoded = account.get("data")
    if not isinstance(encoded, list) or not encoded or encoded[1:] != ["base64"]:
        raise ProgramInspectionError("IDL account returned an unsupported data encoding")
    try:
        raw = base64.b64decode(encoded[0], validate=True)
    except Exception as exc:
        raise ProgramInspectionError("IDL account data is not valid base64") from exc

    idl = None
    for offset in (0, 8):
        if len(raw) < offset + 36:
            continue
        data_len = int.from_bytes(raw[offset + 32:offset + 36], "little")
        if not 0 < data_len <= 10_000_000 or offset + 36 + data_len > len(raw):
            continue
        payload = raw[offset + 36:offset + 36 + data_len]
        try:
            decoded = payload if payload.lstrip().startswith(b"{") else zlib.decompress(payload)
            candidate = json.loads(decoded)
        except (zlib.error, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("instructions"), list):
            idl = candidate
            authority = str(Pubkey.from_bytes(raw[offset:offset + 32]))
            break
    if idl is None:
        raise ProgramInspectionError("published Anchor IDL data is malformed or unsupported")

    instructions = []
    for instruction in idl["instructions"]:
        if not isinstance(instruction, dict):
            continue
        instructions.append({
            "name": str(instruction.get("name", "unnamed")),
            "accounts": [str(account.get("name", "unnamed"))
                         for account in instruction.get("accounts", [])
                         if isinstance(account, dict)],
            "args": [str(arg.get("name", "unnamed"))
                     for arg in instruction.get("args", [])
                     if isinstance(arg, dict)],
        })
    return {
        "program_id": str(program),
        "idl_address": str(idl_address),
        "idl_authority": authority,
        "name": str(idl.get("name", "unknown")),
        "version": str(idl.get("version", "unknown")),
        "instructions": instructions,
    }


def _keypair_from_seed(seed):
    from solders.keypair import Keypair
    return Keypair.from_seed(bytes(seed))


def keypair_from_secret(secret):
    """Parse base58 32-byte seed or 64-byte Solana secret-key array/base58 encoding."""
    text = str(secret).strip()
    try:
        if text.startswith("["):
            raw = bytes(json.loads(text))
            if len(raw) == 64:
                return Keypair.from_bytes(raw)
            if len(raw) == 32:
                return _keypair_from_seed(raw)
            raise ValueError("solana key must contain 32 or 64 bytes")
        from solders.keypair import Keypair
        return Keypair.from_base58_string(text)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("not a valid Solana secret key") from exc
    raise ValueError("Solana secret key must decode to 32 or 64 bytes")


def generate_wallet():
    from solders.keypair import Keypair
    kp = Keypair()
    return kp.to_base58_string(), str(kp.pubkey())


def public_key(secret):
    return str(keypair_from_secret(secret).pubkey())


def validate_secret(secret):
    try:
        keypair_from_secret(secret)
        return True
    except Exception:
        return False


def get_balance(address, *, rpc_url=None, call=rpc_call):
    result = call("getBalance", [address, {"commitment": "confirmed"}], rpc_url=rpc_url)
    return int(result["value"])


def prepare_buy(token_mint, amount_lamports, wallet, *, slippage_bps=200, api_key=None,
                quote_fn=None, build_fn=None):
    from acobot import jupiter
    quote_fn = quote_fn or jupiter.quote
    build_fn = build_fn or jupiter.build_swap
    if not token_mint or not wallet or int(amount_lamports) <= 0:
        raise ValueError("token mint, wallet and positive lamport amount required")
    quote = quote_fn(WSOL_MINT, token_mint, int(amount_lamports), slippage_bps=slippage_bps, api_key=api_key)
    swap = build_fn(quote, wallet, api_key=api_key)
    return {"chain": "solana", "wallet": wallet, "token_mint": token_mint,
            "amount_in": int(amount_lamports), "quote": quote,
            "swap_transaction": swap["swapTransaction"],
            "out_amount": int(quote["outAmount"]),
            "min_out": int(quote["otherAmountThreshold"])}


def wait_for_confirmation(signature, *, rpc_url=None, call=rpc_call, timeout_s=30, poll_interval_s=1):
    deadline = time.monotonic() + timeout_s
    while True:
        result = call("getSignatureStatuses", [
            [signature], {"searchTransactionHistory": True}], rpc_url=rpc_url)
        status = (result.get("value") or [None])[0]
        if status:
            if status.get("err"):
                return False
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_interval_s, remaining))


def execute_prepared(prepared, secret, *, rpc_url=None, call=rpc_call):
    """Only invoke after the final explicit confirmation. Sign Jupiter's serialized transaction."""
    if not prepared or prepared.get("chain") != "solana":
        return False, "wrong chain"
    signature = None
    try:
        from solders.transaction import VersionedTransaction
        kp = keypair_from_secret(secret)
        if str(kp.pubkey()) != prepared["wallet"]:
            return False, "wallet/key mismatch"
        raw = base64.b64decode(prepared["swap_transaction"], validate=True)
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [kp])
        tx_b64 = base64.b64encode(bytes(signed)).decode()
        result = call("sendTransaction", [tx_b64, {"encoding": "base64", "skipPreflight": False,
                                                     "preflightCommitment": "confirmed"}], rpc_url=rpc_url)
        signature = str(result) if result else None
        if not signature:
            return False, "RPC did not accept transaction"
    except Exception as exc:
        return False, f"transaction failed: {type(exc).__name__}"
    try:
        confirmed = wait_for_confirmation(signature, rpc_url=rpc_url, call=call)
        if confirmed is True:
            return True, f"confirmed: {signature}"
        if confirmed is False:
            return False, f"transaction failed on-chain: {signature}"
    except Exception as exc:
        return False, (f"submitted as {signature}, but confirmation lookup failed "
                       f"({type(exc).__name__}); do not resubmit until status is checked")
    return False, (f"submitted as {signature}, but confirmation was not observed within "
                   "30 seconds; do not resubmit until status is checked")


def get_token_balance(owner, mint, *, rpc_url=None, call=rpc_call):
    result = call("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}], rpc_url=rpc_url)
    entries = result.get("value", [])
    raw = 0; decimals = None; symbol = "SPL"; token_account = None
    token_program_id = None
    for entry in entries:
        account = entry.get("account", {})
        program_id = account.get("owner")
        if program_id not in TOKEN_PROGRAM_IDS:
            continue
        info = entry["account"]["data"]["parsed"]["info"]
        amount = info["tokenAmount"]
        raw += int(amount["amount"])
        decimals = int(amount["decimals"])
        token_account = entry["pubkey"]
        token_program_id = program_id
    if decimals is None:
        raise ValueError("no token account for mint")
    return {"raw": raw, "amount": raw / (10 ** decimals), "decimals": decimals,
            "symbol": symbol, "token_account": token_account,
            "token_program_id": token_program_id}


def get_rent_exempt_minimum(_address=None, *, account_size=165, rpc_url=None,
                            call=rpc_call):
    return int(call("getMinimumBalanceForRentExemption", [account_size], rpc_url=rpc_url))


def valid_address(address):
    try:
        from solders.pubkey import Pubkey
        Pubkey.from_string(str(address))
        return True
    except Exception:
        return False


def _raw_amount(amount, decimals):
    if str(amount).lower() == "all":
        return None
    try:
        value = Decimal(str(amount))
    except InvalidOperation as exc:
        raise ValueError("amount must be a positive number or 'all'") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be a positive number or 'all'")
    scaled = value * (10 ** decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount supports at most {decimals} decimal places")
    return int(scaled)


def _associated_token_address(owner, mint, token_program_id=TOKEN_PROGRAM_ID):
    from solders.pubkey import Pubkey
    owner_pk = Pubkey.from_string(owner)
    mint_pk = Pubkey.from_string(mint)
    token_pk = Pubkey.from_string(token_program_id)
    ata_pk = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)
    address, _ = Pubkey.find_program_address(
        [bytes(owner_pk), bytes(token_pk), bytes(mint_pk)], ata_pk)
    return address


def _extension_name(extension):
    if isinstance(extension, str):
        return extension
    if isinstance(extension, dict):
        return str(extension.get("extension", extension.get("type", "")))
    return ""


def _validate_token_2022_extensions(mint_info):
    info = mint_info.get("data", {}).get("parsed", {}).get("info", {})
    extensions = info.get("extensions", [])
    for extension in extensions:
        name = _extension_name(extension)
        if name.lower().replace("_", "").replace("-", "") not in _SUPPORTED_TOKEN_2022_MINT_EXTENSIONS:
            raise ValueError(
                f"Token-2022 extension '{name or 'unknown'}' needs special transfer handling; "
                "this send path does not support it")


def _validate_token_account(account, mint, owner, token_program_id):
    if account.get("owner") != token_program_id:
        raise ValueError("token account belongs to a different token program")
    parsed = account.get("data", {}).get("parsed", {}).get("info", {})
    if parsed.get("mint") != mint or parsed.get("owner") != owner:
        raise ValueError("token account does not match the expected mint and wallet")
    if str(parsed.get("state", "")).lower() == "frozen":
        raise ValueError("token account is frozen and cannot be transferred")
    for extension in parsed.get("extensions", []):
        if _extension_name(extension).lower().replace("_", "").replace("-", "") != "memotransfer":
            continue
        state = extension.get("state", {}) if isinstance(extension, dict) else {}
        if state.get("requireIncomingTransferMemos"):
            raise ValueError("token account requires a memo on incoming transfers")


def plan_transfer(sender, destination, token, amount, *, call=rpc_call):
    """Read-only SOL/SPL transfer plan; never signs or broadcasts."""
    from solders.pubkey import Pubkey

    try:
        sender = str(Pubkey.from_string(sender))
        destination = str(Pubkey.from_string(destination))
    except Exception as exc:
        raise ValueError("invalid Solana wallet address") from exc

    fee_lamports = 5_000
    if token.lower() in ("native", "sol"):
        decimals, symbol, mint = 9, "SOL", None
        raw = _raw_amount(amount, decimals)
        balance = int(call("getBalance", [sender, {"commitment": "confirmed"}])["value"])
        spendable = max(0, balance - fee_lamports)
        transfer_raw = spendable if raw is None else raw
        if transfer_raw <= 0 or transfer_raw > spendable:
            raise ValueError(f"insufficient SOL; available after fee is {spendable / 1e9:.9f} SOL")
        return {
            "chain": "solana", "kind": "native", "symbol": symbol, "decimals": decimals,
            "mint": None, "from": sender, "to": destination, "amount": transfer_raw,
            "amount_text": str(amount), "fee_lamports": fee_lamports, "rent_lamports": 0,
            "token_accounts": [], "create_ata": False,
        }

    try:
        mint = str(Pubkey.from_string(token))
    except Exception as exc:
        raise ValueError("enter a valid Solana SPL token mint") from exc
    mint_info = call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    mint_value = mint_info.get("value")
    if not mint_value:
        raise ValueError("SPL mint account was not found")
    token_program_id = mint_value.get("owner")
    if token_program_id not in TOKEN_PROGRAM_IDS:
        raise ValueError("mint is not owned by the standard SPL Token or Token-2022 program")
    if token_program_id == TOKEN_2022_PROGRAM_ID:
        _validate_token_2022_extensions(mint_value)
    supply = call("getTokenSupply", [mint, {"commitment": "confirmed"}])
    decimals = int(supply["value"]["decimals"])
    symbol = "SPL"
    requested = _raw_amount(amount, decimals)
    response = call("getTokenAccountsByOwner", [
        sender, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    token_accounts = []
    for item in response.get("value", []):
        account = item.get("account", {})
        parsed = account.get("data", {}).get("parsed", {})
        info = parsed.get("info", {})
        token_amount = info.get("tokenAmount", {})
        if account.get("owner") != token_program_id or info.get("mint") != mint:
            continue
        if str(info.get("state", "")).lower() == "frozen":
            continue
        raw_balance = int(token_amount.get("amount", "0"))
        if raw_balance:
            token_accounts.append({"address": item["pubkey"], "amount": raw_balance})
    total = sum(account["amount"] for account in token_accounts)
    transfer_raw = total if requested is None else requested
    if transfer_raw <= 0 or transfer_raw > total:
        raise ValueError(f"insufficient SPL balance; available is {total / (10 ** decimals):g} {symbol}")

    remaining = transfer_raw
    allocations = []
    for account in token_accounts:
        amount_from_account = min(remaining, account["amount"])
        if amount_from_account:
            allocations.append({"address": account["address"], "amount": amount_from_account})
            remaining -= amount_from_account
        if remaining == 0:
            break
    if remaining:
        raise ValueError("SPL token balance changed while building the preview; retry")

    destination_ata = str(_associated_token_address(destination, mint, token_program_id))
    destination_info = call("getAccountInfo", [
        destination_ata, {"encoding": "jsonParsed", "commitment": "confirmed"}])
    destination_value = destination_info.get("value")
    if destination_value:
        dest_parsed = destination_value.get("data", {}).get("parsed", {}).get("info", {})
        _validate_token_account(destination_value, mint, destination, token_program_id)
    rent_size = 170 if token_program_id == TOKEN_2022_PROGRAM_ID else 165
    rent_lamports = 0 if destination_value else get_rent_exempt_minimum(
        account_size=rent_size, call=call)
    sol_balance = int(call("getBalance", [sender, {"commitment": "confirmed"}])["value"])
    if sol_balance < fee_lamports + rent_lamports:
        raise ValueError(
            f"insufficient SOL for transaction fee and recipient token-account rent "
            f"({(fee_lamports + rent_lamports) / 1e9:.6f} SOL required)")
    return {
        "chain": "solana", "kind": "spl", "symbol": symbol, "decimals": decimals,
        "mint": mint, "from": sender, "to": destination, "amount": transfer_raw,
        "amount_text": str(amount), "fee_lamports": fee_lamports,
        "token_program_id": token_program_id,
        "rent_lamports": rent_lamports, "token_accounts": allocations,
        "destination_ata": destination_ata, "create_ata": not destination_value,
    }


def execute_transfer(plan, secret, *, call=rpc_call, confirmed=False):
    """Refresh the plan, then sign and broadcast only after explicit user confirmation."""
    if not confirmed:
        raise PermissionError("final confirmation required")
    if not plan or plan.get("chain") != "solana":
        raise ValueError("invalid Solana transfer plan")
    keypair = keypair_from_secret(secret)
    if str(keypair.pubkey()) != plan["from"]:
        raise ValueError("wallet/key mismatch")

    try:
        refreshed = plan_transfer(plan["from"], plan["to"],
                                  "native" if plan["kind"] == "native" else plan["mint"],
                                  plan["amount_text"], call=call)
    except ValueError as exc:
        if "insufficient" in str(exc).lower():
            raise ValueError(
                "balance changed since preview; transfer is no longer affordable. "
                "nothing sent; prepare it again") from exc
        raise
    if refreshed["amount"] != plan["amount"]:
        raise ValueError("balance changed since preview; nothing sent. prepare the transfer again")

    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import Message
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    from solders.pubkey import Pubkey

    owner = keypair.pubkey()
    instructions = []
    if refreshed["kind"] == "native":
        instructions.append(transfer(TransferParams(
            from_pubkey=owner, to_pubkey=Pubkey.from_string(refreshed["to"]),
            lamports=refreshed["amount"])))
    else:
        mint_pk = Pubkey.from_string(refreshed["mint"])
        destination_ata = Pubkey.from_string(refreshed["destination_ata"])
        if refreshed["create_ata"]:
            associated_program = Pubkey.from_string(ASSOCIATED_TOKEN_PROGRAM_ID)
            instructions.append(Instruction(associated_program, bytes([1]), [
                AccountMeta(owner, True, True),
                AccountMeta(destination_ata, False, True),
                AccountMeta(Pubkey.from_string(refreshed["to"]), False, False),
                AccountMeta(mint_pk, False, False),
                AccountMeta(Pubkey.from_string(SYSTEM_PROGRAM_ID), False, False),
                AccountMeta(Pubkey.from_string(TOKEN_PROGRAM_ID), False, False),
                AccountMeta(Pubkey.from_string("SysvarRent111111111111111111111111111111111"), False, False),
            ]))
        token_program = Pubkey.from_string(refreshed["token_program_id"])
        for allocation in refreshed["token_accounts"]:
            data = bytes([12]) + int(allocation["amount"]).to_bytes(8, "little") + bytes([refreshed["decimals"]])
            instructions.append(Instruction(token_program, data, [
                AccountMeta(Pubkey.from_string(allocation["address"]), False, True),
                AccountMeta(mint_pk, False, False),
                AccountMeta(destination_ata, False, True),
                AccountMeta(owner, True, False),
            ]))

    blockhash_result = call("getLatestBlockhash", [{"commitment": "confirmed"}])
    blockhash = Hash.from_string(blockhash_result["value"]["blockhash"])
    tx = Transaction.new_signed_with_payer(instructions, owner, [keypair], blockhash)
    signature = call("sendTransaction", [
        base64.b64encode(bytes(tx)).decode(),
        {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"},
    ])
    if not signature:
        raise RuntimeError("Solana RPC did not return a transaction signature")
    return str(signature)
