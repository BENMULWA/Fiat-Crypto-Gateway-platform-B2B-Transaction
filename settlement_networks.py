"""Where a merchant's conversion proceeds can be settled.

`automated` networks are sent by the platform when treasury submits the
transfer (existing Celo / Cardano code in routes/otc_admin.py). Every other
network is settled by treasury from their own wallet and recorded with the
transaction hash -- listed here so merchants can name the exact network, but
never faked as automatic.
"""
import re

SETTLEMENT_CHANNELS = {"WALLET_BALANCE", "TO_BANK", "TO_EXTERNAL_WALLET"}

_EVM = r"^0x[a-fA-F0-9]{40}$"

NETWORKS: dict[str, dict] = {
    "celo": {"label": "Celo Network", "assets": ["cUSD", "USDC", "USDT"], "automated": True, "pattern": _EVM, "hint": "0x... (Celo address)"},
    "cardano": {"label": "Cardano", "assets": ["USDA"], "automated": True, "pattern": r"^(addr1|addr_test1)[0-9a-z]{40,}$", "hint": "addr1..."},
    "stellar": {"label": "Stellar", "assets": ["USDC"], "automated": False, "pattern": r"^G[A-Z2-7]{55}$", "hint": "G... (56 characters)"},
    "polygon": {"label": "Polygon", "assets": ["USDT", "USDC"], "automated": False, "pattern": _EVM, "hint": "0x... (Polygon address)"},
    "bsc": {"label": "BNB Chain (BEP20)", "assets": ["USDT", "USDC"], "automated": False, "pattern": _EVM, "hint": "0x... (BEP20 address)"},
    "tron": {"label": "Tron (TRC20)", "assets": ["USDT"], "automated": False, "pattern": r"^T[1-9A-HJ-NP-Za-km-z]{33}$", "hint": "T... (34 characters)"},
    "ethereum": {"label": "Ethereum (ERC20)", "assets": ["USDT", "USDC"], "automated": False, "pattern": _EVM, "hint": "0x... (Ethereum address)"},
}

COUNTRIES: list[dict] = [
    {"code": "KE", "name": "Kenya", "currency": "KES"}, {"code": "UG", "name": "Uganda", "currency": "UGX"},
    {"code": "TZ", "name": "Tanzania", "currency": "TZS"}, {"code": "RW", "name": "Rwanda", "currency": "RWF"},
    {"code": "BI", "name": "Burundi", "currency": "BIF"}, {"code": "NG", "name": "Nigeria", "currency": "NGN"},
    {"code": "GH", "name": "Ghana", "currency": "GHS"}, {"code": "ZA", "name": "South Africa", "currency": "ZAR"},
    {"code": "ET", "name": "Ethiopia", "currency": "ETB"}, {"code": "MW", "name": "Malawi", "currency": "MWK"},
    {"code": "ZM", "name": "Zambia", "currency": "ZMW"}, {"code": "SN", "name": "Senegal", "currency": "XOF"},
    {"code": "CI", "name": "Cote d'Ivoire", "currency": "XOF"}, {"code": "CM", "name": "Cameroon", "currency": "XAF"},
    {"code": "GB", "name": "United Kingdom", "currency": "GBP"}, {"code": "DE", "name": "Germany", "currency": "EUR"},
    {"code": "FR", "name": "France", "currency": "EUR"}, {"code": "US", "name": "United States", "currency": "USD"},
    {"code": "CN", "name": "China", "currency": "CNY"}, {"code": "HK", "name": "Hong Kong", "currency": "CNH"},
]

FIAT_CURRENCIES = {c["currency"] for c in COUNTRIES}


def is_fiat(asset: str) -> bool:
    return str(asset or "").upper() in FIAT_CURRENCIES


def validate_wallet_address(network: str, address: str) -> bool:
    cfg = NETWORKS.get(network)
    return bool(cfg and re.match(cfg["pattern"], str(address or "").strip()))


def network_supports_asset(network: str, asset: str) -> bool:
    cfg = NETWORKS.get(network)
    return bool(cfg) and str(asset or "").upper() in {a.upper() for a in cfg["assets"]}


def public_options() -> dict:
    return {
        "networks": [
            {"id": k, "label": v["label"], "assets": v["assets"], "automated": v["automated"], "hint": v["hint"]}
            for k, v in NETWORKS.items()
        ],
        "countries": COUNTRIES,
    }
