import os, re, tomllib
from dataclasses import dataclass, field

SUPPORTED_EVM_CHAIN_IDS = {
    "ethereum": 1,
    "robinhood": 4663,
    "ink": 57073,
}
SOLANA_RPC_DEFAULT = "https://api.mainnet-beta.solana.com"
ALCHEMY_ENDPOINTS = {
    "ethereum": ("https://eth-mainnet.g.alchemy.com/v2/{key}",
                 "wss://eth-mainnet.g.alchemy.com/v2/{key}"),
    "robinhood": ("https://robinhood-mainnet.g.alchemy.com/v2/{key}",
                  "wss://robinhood-mainnet.g.alchemy.com/v2/{key}"),
}
ALCHEMY_SOLANA_ENDPOINT = "https://solana-mainnet.g.alchemy.com/v2/{key}"
HELIUS_SOLANA_ENDPOINT = "https://mainnet.helius-rpc.com/?api-key={key}"
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_endpoint(value):
    missing = False

    def replace(match):
        nonlocal missing
        replacement = os.environ.get(match.group(1))
        if not replacement:
            missing = True
            return ""
        return replacement

    endpoint = _ENV_REF.sub(replace, str(value)).strip()
    return None if missing or not endpoint else endpoint


def _endpoint_list(values):
    return [endpoint for value in values or []
            if (endpoint := _expand_endpoint(value)) is not None]


def _prefer(endpoint, endpoints):
    return list(dict.fromkeys([endpoint, *endpoints]))


def _environment_endpoints(variable):
    value = os.environ.get(variable, "")
    if not value:
        return []
    return _endpoint_list(value.split(","))


@dataclass
class Chain:
    name: str
    chain_id: int
    rpc: list
    broadcast_rpc: list
    ws_rpc: list = field(default_factory=list)
    priority_fee_gwei: float = 2.0
    max_fee_gwei: float = 50.0
    gas_limit: int = 250000

@dataclass
class Config:
    admin_ids: list
    whitelist_ids: list
    whitelist_usernames: list = field(default_factory=list)
    chains: dict = field(default_factory=dict)
    solana_rpc_url: str = SOLANA_RPC_DEFAULT
    solana_rpc_urls: list = field(default_factory=lambda: [SOLANA_RPC_DEFAULT])
    telegram_token: str = ""
    saved_tokens: dict = field(default_factory=dict)

def load(path="config.toml") -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    chains = {}
    for name, c in raw.get("chains", {}).items():
        if name == "robinhood_testnet":
            continue
        if name not in SUPPORTED_EVM_CHAIN_IDS:
            raise ValueError(f"unsupported EVM network in config: {name}")
        chain_id = c["chain_id"]
        if chain_id != SUPPORTED_EVM_CHAIN_IDS[name]:
            raise ValueError(
                f"chain '{name}' must use chain ID {SUPPORTED_EVM_CHAIN_IDS[name]}, got {chain_id}")
        rpc = _endpoint_list(c.get("rpc", []))
        broadcast_rpc = _endpoint_list(c.get("broadcast_rpc", []))
        ws_rpc = _endpoint_list(c.get("ws_rpc", []))
        alchemy_key = os.environ.get("ALCHEMY_API_KEY", "").strip()
        if alchemy_key and name in ALCHEMY_ENDPOINTS:
            alchemy_rpc, alchemy_ws = (template.format(key=alchemy_key)
                                       for template in ALCHEMY_ENDPOINTS[name])
            rpc = _prefer(alchemy_rpc, rpc)
            broadcast_rpc = _prefer(alchemy_rpc, broadcast_rpc)
            ws_rpc = _prefer(alchemy_ws, ws_rpc)
        env_prefix = name.upper()
        env_primary = _environment_endpoints(f"{env_prefix}_RPC_URL")
        env_backups = _environment_endpoints(f"{env_prefix}_RPC_BACKUP_URLS")
        if env_primary:
            rpc = list(dict.fromkeys([*env_primary, *env_backups, *rpc]))
            broadcast_rpc = list(dict.fromkeys([*env_primary, *env_backups, *broadcast_rpc]))
        elif env_backups:
            rpc = list(dict.fromkeys([*rpc, *env_backups]))
            broadcast_rpc = list(dict.fromkeys([*broadcast_rpc, *env_backups]))
        if not rpc:
            raise ValueError(f"chain '{name}' has no RPC endpoint configured")
        if not broadcast_rpc:
            broadcast_rpc = list(rpc)
        chains[name] = Chain(name=name, chain_id=chain_id, rpc=rpc,
                             broadcast_rpc=broadcast_rpc,
                             ws_rpc=ws_rpc,
                             priority_fee_gwei=c.get("priority_fee_gwei", 2.0),
                             max_fee_gwei=c.get("max_fee_gwei", 50.0),
                             gas_limit=c.get("gas_limit", 250000))
    solana_cfg = raw.get("solana", {})
    solana_env_primary = _environment_endpoints("SOLANA_RPC_URL")
    solana_cfg_primary = _expand_endpoint(solana_cfg.get("rpc_url", SOLANA_RPC_DEFAULT))
    alchemy_key = os.environ.get("ALCHEMY_API_KEY", "").strip()
    helius_key = os.environ.get("HELIUS_API_KEY", "").strip()
    provider_backups = []
    if alchemy_key:
        provider_backups.append(ALCHEMY_SOLANA_ENDPOINT.format(key=alchemy_key))
    if helius_key:
        provider_backups.append(HELIUS_SOLANA_ENDPOINT.format(key=helius_key))
    solana_rpc_url = (solana_env_primary[0] if solana_env_primary
                      else (provider_backups[0] if provider_backups else solana_cfg_primary))
    if solana_rpc_url is None:
        solana_rpc_url = SOLANA_RPC_DEFAULT
    solana_backups = [
        *solana_env_primary[1:],
        *provider_backups,
        *([solana_cfg_primary] if solana_cfg_primary else []),
        *_endpoint_list(solana_cfg.get("backup_rpc_urls", [])),
        *_environment_endpoints("SOLANA_RPC_BACKUP_URLS"),
    ]
    solana_rpc_urls = list(dict.fromkeys([solana_rpc_url, *solana_backups]))
    return Config(admin_ids=raw.get("admin_ids", []), whitelist_ids=raw.get("whitelist_ids", []), whitelist_usernames=raw.get("whitelist_usernames", []),
                  solana_rpc_url=solana_rpc_url,
                  solana_rpc_urls=solana_rpc_urls,
                  chains=chains, telegram_token=os.environ.get(raw.get("telegram_token_env", "ACO_BOT_TOKEN"), "") or raw.get("secrets", {}).get("telegram_token", ""),
                  saved_tokens=raw.get("saved_tokens", {
        "ethereum": [{"symbol": "ETH", "contract": "native"}, {"symbol": "USDT", "contract": "0xdAC17F958D2ee523a2206206994597C13D831ec7"}, {"symbol": "USDC", "contract": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"}],
        "solana": [{"symbol": "SOL", "contract": "native"}],
    }))
