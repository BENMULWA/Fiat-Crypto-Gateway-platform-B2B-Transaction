from .bech32 import bech32_decode, bech32_encode


def minimum_utxo(params, assets=[]) -> int:
    """Calculate the minimum UTxO value when assets are part of the
    transaction.

    Parameters
    ----------
    params : dict
        A dictionary of protocol parameters.
    assets : list, optional
        A list of assets in the format policyid.name.

    Returns
    -------
    int
        The minimum transaction output (Lovelace).
    """

    # Round the number of bytes to the minimum number of 8 byte words needed
    # to hold all the bytes.
    def round_up_bytes_to_words(b):
        return (b + 7) // 8

    # These are constants but may change in the future
    coin_Size = 2
    utxo_entry_size_without_val = 27
    ada_only_utxo_size = utxo_entry_size_without_val + coin_Size
    pid_size = 28

    # Get the minimum UTxO parameter
    # Babbage era changed utxoCostPerWord to utxoCostPerByte
    if params.get("utxoCostPerWord"):
        utxo_cost_word = params.get("utxoCostPerWord")
    else:
        utxo_cost_word = 8*params.get("utxoCostPerByte")
    min_utxo = ada_only_utxo_size * utxo_cost_word
    if len(assets) == 0:
        return min_utxo

    # Get lists of unique policy IDs and asset names.
    unique_pids = list(set([asset.split(".")[0] for asset in assets]))
    unique_names = list(set([asset.split(".")[1] for asset in assets if len(asset.split(".")) > 1]))

    # Get the number of unique policy IDs and token names in the bundle
    num_pids = len(unique_pids)
    num_assets = max([len(unique_names), 1])

    # The sum of the length of the ByteStrings representing distinct asset names
    sum_asset_name_lengths = sum([len(s.encode("utf-8")) for s in unique_names])
    [s.encode("utf-8") for s in unique_names]

    # The size of the token bundle in 8-byte long words
    size_bytes = 6 + round_up_bytes_to_words(
        (num_assets * 12) + sum_asset_name_lengths + (num_pids * pid_size)
    )

    return max(
        [
            min_utxo,
            (min_utxo // ada_only_utxo_size) * (utxo_entry_size_without_val + size_bytes),
        ]
    )


__all__ = ["minimum_utxo", "bech32_decode", "bech32_encode"]
