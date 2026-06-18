"""
Blockfrost chain context — single instance reused across requests.
Raises RuntimeError on first use if BLOCKFROST_PROJECT_ID is not configured
or if the blockfrost/pycardano packages are not installed.
"""
from __future__ import annotations

from functools import lru_cache
from config import settings

_NETWORK_MAP = {
    "mainnet": ("https://cardano-mainnet.blockfrost.io/api", "mainnet"),
    "preprod": ("https://cardano-preprod.blockfrost.io/api", "testnet"),
    "preview":  ("https://cardano-preview.blockfrost.io/api",  "testnet"),
}


def _require_blockfrost() -> str:
    if not settings.blockfrost_project_id:
        raise RuntimeError(
            "BLOCKFROST_PROJECT_ID is not set. "
            "Add it to your .env file to enable Cardano features."
        )
    return settings.blockfrost_project_id


@lru_cache(maxsize=1)
def get_blockfrost_api():
    try:
        from blockfrost import BlockFrostApi, ApiUrls
    except ImportError:
        raise RuntimeError("blockfrost-python is not installed. Run: pip install blockfrost-python")

    project_id = _require_blockfrost()
    base_url_key, _ = _NETWORK_MAP.get(settings.cardano_network, _NETWORK_MAP["preprod"])

    url_map = {
        "mainnet": ApiUrls.mainnet.value,
        "preprod": ApiUrls.preprod.value,
        "preview":  ApiUrls.preview.value,
    }
    base_url = url_map.get(settings.cardano_network, ApiUrls.preprod.value)
    return BlockFrostApi(project_id=project_id, base_url=base_url)


@lru_cache(maxsize=1)
def get_chain_context():
    try:
        from pycardano import BlockFrostChainContext, Network
        from blockfrost import ApiUrls
    except ImportError:
        raise RuntimeError("pycardano or blockfrost-python is not installed.")

    project_id = _require_blockfrost()
    network_map = {
        "mainnet": (ApiUrls.mainnet.value, Network.MAINNET),
        "preprod": (ApiUrls.preprod.value, Network.TESTNET),
        "preview":  (ApiUrls.preview.value, Network.TESTNET),
    }
    base_url, network = network_map.get(settings.cardano_network, network_map["preprod"])
    return BlockFrostChainContext(project_id=project_id, network=network)


def get_network():
    try:
        from pycardano import Network
    except ImportError:
        raise RuntimeError("pycardano is not installed.")

    _, net_str = _NETWORK_MAP.get(settings.cardano_network, _NETWORK_MAP["preprod"])
    from pycardano import Network
    return Network.MAINNET if net_str == "mainnet" else Network.TESTNET
