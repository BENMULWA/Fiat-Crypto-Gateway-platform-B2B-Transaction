import json
import logging
import shlex
import subprocess
import time
from collections import namedtuple
from pathlib import Path

import pexpect
import requests

# Cardano-Tools components
from .utils import minimum_utxo


class WalletError(Exception):
    pass


class WalletHTTP:
    """While cardano-wallet provides 2 APIs, HTTP and CLI, the HTTP API has more features, so we
    primarily support HTTP with this library. For full specifications on the use of these commands,
    refer to the cardano-wallet HTTP API documentation: https://input-output-hk.github.io/cardano-wallet/api/edge/
    """

    def __init__(self, wallet_server: str = "http://localhost", wallet_server_port: int = 8090):
        self.wallet_url = f"{wallet_server}:{wallet_server_port}/"
        self.logger = logging.getLogger(__name__)

    def get_settings(self) -> dict:
        """Returns wallet server settings"""
        url = f"{self.wallet_url}v2/settings"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def update_settings(self, smash_source: str) -> None:
        """Updates wallet server settings. Currently, the only setting is SMASH server URL"""
        url = f"{self.wallet_url}v2/settings"
        headers = {"Content-type": "application/json"}
        payload = {"settings": {"pool_metadata_source": "direct"}}
        r = requests.put(url, headers=headers, json=payload)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
        return

    def get_smash_health(self) -> dict:
        """Get health status of currently active SMASH server"""
        url = f"{self.wallet_url}v2/smash/health"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def get_network_info(self) -> dict:
        """Returns network information"""
        url = f"{self.wallet_url}v2/network/information"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def get_network_clock(self, force_ntp_check: bool = False) -> dict:
        """Returns network clock status"""
        url = f"{self.wallet_url}v2/network/clock?forceNtpCheck={force_ntp_check}"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def get_network_params(self) -> dict:
        """Returns the set of network parameters for the current epoch."""
        url = f"{self.wallet_url}v2/network/parameters"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def get_latest_block_header(self) -> dict:
        """Returns the latest block header available at the chain source"""
        url = f"{self.wallet_url}v2/blocks/latest/header"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def create_wallet(
        self,
        name: str,
        recovery_phrase: list[str],
        passphrase: str,
        secondary_phrase: list[str] = None,
        address_pool_gap: int = 20,
    ):
        url = f"{self.wallet_url}v2/wallets"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        tx_body = {
            "name": name,
            "mnemonic_sentence": recovery_phrase,
            "mnemonic_second_factor": secondary_phrase,
            "passphrase": passphrase,
            "address_pool_gap": address_pool_gap,
        }
        r = requests.post(url, json=tx_body, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def create_wallet_from_key(
        self,
        name: str,
        xpub_key: list[str],
        address_pool_gap: int = 20,
    ):
        """Creates/restores wallet from an extended public key (account public key + chain code)"""
        url = f"{self.wallet_url}v2/wallets"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        tx_body = {
            "name": name,
            "account_public_key": xpub_key,
            "address_pool_gap": address_pool_gap,
        }
        r = requests.post(url, json=tx_body, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def rename_wallet(self, wallet_id: str, name: str) -> dict:
        """Changes the name of the specified wallet"""
        url = f"{self.wallet_url}v2/wallets/{wallet_id}"
        self.logger.debug(f"URL: {url}")
        headers = {"Content-type": "application/json"}
        payload = {"name": name}
        r = requests.put(url, headers=headers, json=payload)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def update_passphrase(self, wallet_id: str, old_passphrase: str, new_passphrase: str) -> bool:
        """Changes the name of the specified wallet"""
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/passphrase"
        self.logger.debug(f"URL: {url}")
        headers = {"Content-type": "application/json"}
        payload = {"old_passphrase": old_passphrase, "new_passphrase": new_passphrase}
        r = requests.put(url, headers=headers, json=payload)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return False
        return True

    def delete_wallet(self, wallet_id: str) -> None:
        url = f"{self.wallet_url}v2/wallets/{wallet_id}"
        self.logger.debug(f"URL: {url}")
        r = requests.delete(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")

    def get_all_wallets(self) -> dict:
        """Get a list of all created wallets known to the wallet service.

        Returns
        ----------
        list
            List of dicts each representing the wallet info.
        """
        url = f"{self.wallet_url}v2/wallets"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def get_wallet(self, wallet_id: str) -> dict:
        """Find the wallet specified by the ID.

        Parameters
        ----------
        wallet_id : str
            The wallet ID.
        """
        url = f"{self.wallet_url}v2/wallets/{wallet_id}"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def get_wallet_by_name(self, name: str) -> dict:
        """Find the wallet from the supplied name (case insensitive).

        Parameters
        ----------
        name : str
            The arbitrary name of the wallet supplied during creation.
        """

        # First get a list of all wallets known to the local install.
        all_wallets = self.get_all_wallets()
        for wallet in all_wallets:
            if wallet.get("name").lower() == name.lower():
                return wallet
        return {}

    def get_balance(self, wallet_id: str) -> tuple:
        """Get balances of wallet"""
        url = f"{self.wallet_url}v2/wallets/{wallet_id}"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return ()
        payload = json.loads(r.text)
        lovelace_balance = payload.get("balance").get("total")
        asset_balances = payload.get("assets").get("total")
        return lovelace_balance, asset_balances

    def get_utxo_stats(self, wallet_id: str) -> tuple:
        """Get wallet's UTxO distribution statistics"""
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/statistics/utxos"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return ()
        stats = json.loads(r.text)
        return stats

    def get_utxo_snapshot(self, wallet_id: str) -> tuple:
        """Get wallet's UTxO snapshot"""
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/utxo"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return ()
        stats = json.loads(r.text)
        return stats

    def get_addresses(self, wallet_id: str) -> list:
        """Returns a list of addresses tracked by the provided wallet"""
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/addresses"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return []
        payload = json.loads(r.text)
        addresses = [elem.get("id") for elem in payload]
        return addresses

    def inspect_address(self, address: str) -> dict:
        """Get useful information about the structure of an address"""
        url = f"{self.wallet_url}v2/addresses/{address}"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return []
        payload = json.loads(r.text)
        return payload

    def get_transaction(self, wallet_id: str, tx_id: str) -> dict:
        """Pull information about the specified transaction."""
        self.logger.info(f"Querying information for transaction {tx_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/transactions/{tx_id}"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def get_transactions(self, wallet_id: str) -> dict:
        """List all transactions for the given wallet"""
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/transactions"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def forget_transaction(self, wallet_id: str, tx_id: str) -> None:
        """Attempt to forget a pending transaction."""
        self.logger.info(f"Forgetting transaction {tx_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/transactions/{tx_id}"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
        return

    def confirm_tx(
        self, wallet_id: str, tx_id: str, timeout: float = 600, pause: float = 5
    ) -> bool:
        """Checks the given transaction and waits until it's submitted."""
        start_time = time.time()
        while True:
            tx_data = self.get_transaction(wallet_id, tx_id)
            self.logger.info(f"TX status: {tx_data.get('status')}")
            if tx_data.get("status") == "in_ledger":
                return True
            if tx_data.get("status") == "expired":
                return False
            if time.time() - start_time > timeout:
                raise WalletError("Timeout waiting for transaction confirmation.")
            self.logger.info("Transaction not yet confirmed, pausing before next check...")
            time.sleep(pause)

    def get_assets(self, wallet_id: str) -> dict:
        """List all assets associated with the wallet (i.e. assets that have ever been spendable by the wallet)"""
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/assets"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def get_asset(self, wallet_id: str, policy_id: str, asset_name: str = None) -> dict:
        """Fetch a single asset associated with the wallet (i.e. must have at one point been spendable by the wallet)"""
        if asset_name:
            url = f"{self.wallet_url}v2/wallets/{wallet_id}/assets/{policy_id}/{asset_name}"
        else:
            url = f"{self.wallet_url}v2/wallets/{wallet_id}/assets/{policy_id}"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def estimate_tx_fee(
        self,
        wallet_id: str,
        rx_address: str,
        quantity: int,
    ) -> dict:
        """Estimate the fee for a transaction"""
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/payment-fees"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        tx_body = {
            "payments": [
                {
                    "address": rx_address,
                    "amount": {"quantity": quantity, "unit": "lovelace"},
                }
            ],
            "withdrawal": "self",
        }
        self.logger.debug(
            f"Estimate fees for sending {quantity:,} lovelace ({quantity / 1e6} ADA) to address {rx_address}..."
        )
        r = requests.post(url, json=tx_body, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        return payload

    def send_lovelace(
        self,
        wallet_id: str,
        rx_address: str,
        quantity: int,
        passphrase: str,
        wait: bool = False,
    ) -> dict:
        """Sends the specified amount of lovelace to the provided address"""
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/transactions"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        tx_body = {
            "passphrase": passphrase,
            "payments": [
                {
                    "address": rx_address,
                    "amount": {"quantity": quantity, "unit": "lovelace"},
                }
            ],
            "withdrawal": "self",
        }
        self.logger.debug(
            f"Sending {quantity:,} lovelace ({quantity / 1e6} ADA) to address {rx_address}..."
        )
        r = requests.post(url, json=tx_body, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        if wait:
            tx_id = payload.get("id")
            self.confirm_tx(wallet_id, tx_id)
            return self.get_transaction(wallet_id, tx_id)
        return payload

    def send_ada(
        self,
        wallet_id: str,
        rx_address: str,
        quantity_ada: int,
        passphrase: str,
        wait: bool = False,
    ) -> dict:
        """Sends the specified amount of ADA to the provided address"""
        return self.send_lovelace(wallet_id, rx_address, quantity_ada * 1_000_000, passphrase, wait)

    def send_tokens(
        self,
        wallet_id: str,
        rx_address: str,
        assets: list,
        passphrase: str,
        lovelace_amount: int = 0,
        wait: bool = False,
    ) -> dict:
        """Sends the specified amount of tokens to the provided address

        assets is a list of dicts comprised of the following:
          {
              "policy_id": str, # unique mint value
              "asset_name": str, # token_id
              "quantity": int # 1
          }

        Note: There is a minimum amount of lovelace that must be included with
              token transactions. If the specified amount is less than this
              minimum value, it will be automatically calculated.
        """

        # Make sure we send at least the minimum lovelace amount
        min_lovelace = minimum_utxo(
            {
                "utxoCostPerWord": 34482,  # Const. from Alonzo genesis file
            },
            [f"{asset.get('policy_id')}.{asset.get('asset_name')}" for asset in assets]
        )
        if lovelace_amount < min_lovelace:
            lovelace_amount = min_lovelace

        url = f"{self.wallet_url}v2/wallets/{wallet_id}/transactions"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        tx_body = {
            "passphrase": passphrase,
            "payments": [
                {
                    "address": rx_address,
                    "amount": {"quantity": lovelace_amount, "unit": "lovelace"},
                    "assets": assets,
                }
            ],
            "withdrawal": "self",
        }
        self.logger.info(
            f"Sending {len(assets)} unique tokens and {lovelace_amount:,} lovelace ({lovelace_amount / 1e6} ADA) to address {rx_address}..."
        )
        r = requests.post(url, json=tx_body, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}

        payload = json.loads(r.text)
        self.logger.debug(f"Tokens sent! Payload {payload}")
        if wait:
            tx_id = payload.get("id")
            self.confirm_tx(wallet_id, tx_id)
            return self.get_transaction(wallet_id, tx_id)
        return payload

    def send_batch_tx(
        self,
        wallet_id: str,
        payments: list,
        passphrase: str,
        wait: bool = False,
    ) -> dict:
        """Sends a batch of transactions. Takes in a list of payments dicts of the following format:
        [
            {
                "address": "addr...",
                "amount": {
                    "quantity": <int>,
                    "unit": "lovelace"
                },
                "assets": [
                    {
                        "policy_id": <hex string>,
                        "asset_name": <str>, # ASCII-formatted hex string
                        "quantity": <int>
                    }
                ]
            }
        ]
        """
        for payment in payments:
            # Make sure we send at least the minimum lovelace amount
            assets = payment.get("assets") if "assets" in payment.keys() else []
            lovelace_amount = payment.get("amount").get("quantity")
            min_lovelace = minimum_utxo(
                {
                    "utxoCostPerWord": 34482,  # Const. from Alonzo genesis file
                },
                [f"{asset.get('policy_id')}.{asset.get('asset_name')}" for asset in assets],
            )
            if lovelace_amount < min_lovelace:
                payment["amount"]["quantity"] = min_lovelace

        url = f"{self.wallet_url}v2/wallets/{wallet_id}/transactions"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        tx_body = {
            "passphrase": passphrase,
            "payments": payments,
            "withdrawal": "self",
        }
        self.logger.debug(f"Sending batch of {len(payments)} payments...")
        r = requests.post(url, json=tx_body, headers=headers)
        if not r.ok:
            self.logger.error(f"ERROR: Bad status code received: {r.status_code}, {r.text}")
            return {}

        payload = json.loads(r.text)
        self.logger.debug(f"Tokens sent! Payload {payload}")
        if wait:
            tx_id = payload.get("id")
            self.confirm_tx(wallet_id, tx_id)
            return self.get_transaction(wallet_id, tx_id)
        return payload

    def construct_transaction(self, wallet_id: str, payload: dict) -> dict:
        """Create a transaction to be signed from the wallet.
        For simple transactions, you can use the send_ada or send_lovelace functions.
        This function provides the ability to send batch transactions of ADA and tokens,
        as well as minting/burning tokens, and stake delegation. See cardano-wallet API for
        more information.

        Expects a payload dict of the following format:

        {
          "payments": [
            {
              "address": "addr1sjck9mdmfyhzvjhydcjllgj9vjvl522w0573ncustrrr2rg7h9azg4cyqd36yyd48t5ut72hgld0fg2xfvz82xgwh7wal6g2xt8n996s3xvu5g",
              "amount": {
                "quantity": 42000000,
                "unit": "lovelace"
              },
              "assets": [
                {
                  "policy_id": "65ab82542b0ca20391caaf66a4d4d7897d281f9c136cd3513136945b",
                  "asset_name": "",
                  "quantity": 0
                }
              ]
            }
          ],
          "withdrawal": "self",
          "metadata": {
            "0": {
              "string": "cardano"
            },
              "int": 14
            "1": {
              "bytes": "2512a00e9653fe49a44a5886202e24d77eeb998f"
            }
          },
          "mint_burn": [
            {
              "policy_script_template": "string",
              "asset_name": "",
              "operation": {
                "mint": {
                  "receiving_address": "addr1sjck9mdmfyhzvjhydcjllgj9vjvl522w0573ncustrrr2rg7h9azg4cyqd36yyd48t5ut72hgld0fg2xfvz82xgwh7wal6g2xt8n996s3xvu5g",
                  "quantity": 0
                }
              }
            }
          ],
          "delegations": [
            {
              "join": {
                "pool": "pool1wqaz0q0zhtxlgn0ewssevn2mrtm30fgh2g7hr7z9rj5856457mm",
                "stake_key_index": "1852H"
              }
            }
          ],
          "validity_interval": {
            "invalid_before": {
              "quantity": 10,
              "unit": "second"
            },
            "invalid_hereafter": {
              "quantity": 10,
              "unit": "second"
            }
          },
          "encoding": "base16"
        }
        """
        self.logger.info(f"Constructing new transaction for wallet {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/transactions-construct"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        self.logger.debug(f"Constructing transaction with the following payload: {payload}")
        r = requests.post(url, json=payload, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def sign_transaction(self, wallet_id: str, passphrase: str, tx: str) -> dict:
        """Sign a serialized transaction (i.e. output of construct_transaction).
        Returns the signed transaction."""
        self.logger.info(f"Signing serialized transaction for wallet ID {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/transactions-sign"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        payload = {"passphrase": passphrase, "transaction": tx}
        r = requests.post(url, json=payload, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def decode_transaction(self, wallet_id: str, tx: str) -> dict:
        """Decode a serialized transaction (e.g. output of construct_transaction)."""
        self.logger.info(f"Decoding serialized transaction for wallet ID {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/transactions-decode"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        payload = {"transaction": tx}
        r = requests.post(url, json=payload, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def submit_transaction(self, wallet_id: str, tx: str) -> dict:
        """Submit a signed, serialized transaction (e.g. output of sign_transaction)."""
        self.logger.info(f"Submitting transaction for wallet ID {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/transactions-submit"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        payload = {"transaction": tx}
        r = requests.post(url, json=payload, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def create_migration_plan(self, wallet_id: str, dest_addresses: list) -> dict:
        """Creates a plan for migrating the full UTxO balance from the specified wallet to another wallet."""
        self.logger.info(f"Creating migration plan for wallet ID {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/migrations/plan"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        payload = {"addresses": dest_addresses}
        r = requests.post(url, json=payload, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def migrate_wallet(self, wallet_id: str, passphrase: str, dest_addresses: list) -> dict:
        """Migrates the full UTxO balance from the specified wallet to another wallet."""
        self.logger.info(f"Migrating wallet ID {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/migrations"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        payload = {"passphrase": passphrase, "addresses": dest_addresses}
        r = requests.post(url, json=payload, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def list_stake_keys(self, wallet_id: str) -> dict:
        """List stake keys relevant to the wallet, and how much ada is associated with each."""
        self.logger.debug(f"Listing stake keys for wallet ID {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/stake-keys"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def list_stake_pools(self, lovelace_to_stake: int) -> dict:
        """List all known stake pools, ordered by descending non_myopic_member_rewards"""
        self.logger.debug(
            f"Listing stake pools, ordered for stake amount of {lovelace_to_stake} lovelace"
        )
        url = f"{self.wallet_url}v2/stake-pools?stake={lovelace_to_stake}"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def pool_maintenance_actions(self) -> dict:
        """View the status of stake pool maintenance actions for the local node"""
        self.logger.debug(f"Viewing stake pool maintenance actions.")
        url = f"{self.wallet_url}v2/stake-pools/maintenance-actions"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def trigger_pool_maintenance(self, action: str) -> None:
        """Performs maintenance actions on stake pools for the local node
        (e.g. based on the output of pool_maintenance_actions)"""
        self.logger.info(f"Performing stake pool maintenance action: {action}")
        url = f"{self.wallet_url}v2/stake-pools/maintenance-actions"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        payload = {"maintenance_action": action}
        r = requests.post(url, json=payload, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
        return

    def estimate_delegation_fee(self, wallet_id: str) -> dict:
        """Estimate fee for joining or leaving a stake pool."""
        self.logger.debug(f"Estimating delegation fee for wallet {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/delegation-fees"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def join_stake_pool(self, wallet_id: str, passphrase: str, pool_id: str) -> None:
        """Delegate all addresses from the given wallet to the given stake pool"""
        self.logger.debug(f"Delegating wallet {wallet_id} to stake pool {pool_id}")
        url = f"{self.wallet_url}v2/stake-pools/{pool_id}/wallets/{wallet_id}"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        payload = {"passphrase": passphrase}
        r = requests.put(url, json=payload, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
        return

    def quit_staking(self, wallet_id: str, passphrase: str) -> dict:
        """Stop delegating completely. The wallet's stake will become inactive and
        rewards will be withdrawn automatically"""
        self.logger.debug(f"Stopping delegation for wallet {wallet_id}")
        url = f"{self.wallet_url}v2/stake-pools/*/wallets/{wallet_id}"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        payload = {"passphrase": passphrase}
        r = requests.delete(url, json=payload, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def create_account_public_key(
        self,
        wallet_id: str,
        index: str,
        passphrase: str,
        format: str = "non_extended",
        purpose: str = "1852H",
    ) -> dict:
        """Derive an account public key for any account index. For this key
        derivation to be possible, the wallet must have been created from mnemonic."""
        self.logger.info(f"Deriving account public key for wallet {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/keys/{index}"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        payload = {"passphrase": passphrase, "format": format, "purpose": purpose}
        r = requests.post(url, json=payload, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def get_account_public_key(self, wallet_id: str) -> dict:
        """Retrieve the account public key of this wallet"""
        self.logger.debug(f"Retrieving account public key for wallet {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/keys"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def get_public_key(self, wallet_id: str, role: str, index: str) -> dict:
        """Retrieve the public key for the given role and derivation index of this wallet.
        Options for role are: utxo_external, utxo_internal, or mutable_account."""
        self.logger.debug(f"Retrieving public key for wallet {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/keys/{role}/{index}"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def create_policy_id(self, wallet_id: str, policy_script_template: dict) -> dict:
        """Create a new policy ID for the wallet. See cardano-wallet documentation for specifics about the policy_script_template format.
        To create a policy signed by only this wallet, you can simply provide the string 'cosigner#0'.

        Note: 'cosigner#0' stands for our wallet’s policy key. In case of Shelley wallet we have only one. In the future, in the Shared
        wallets, we’ll be able to construct a minting/burning script with many policy keys shared between different users and they will
        be identified as 'cosigner#1', 'cosigner#2', etc"""
        self.logger.debug(f"Creating policy ID for wallet {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/policy-id"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        payload = {"policy_script_template": policy_script_template}
        r = requests.post(url, json=payload, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def create_policy_key(
        self,
        wallet_id: str,
        passphrase: str,
        hash_format: bool = False,
    ) -> dict:
        """Create policy key for the wallet. hash_format = True returns a hash of the key instead."""
        self.logger.info(f"Creating policy key for wallet {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/policy-key?hash={hash_format}"
        self.logger.debug(f"URL: {url}")
        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }
        payload = {"passphrase": passphrase}
        r = requests.post(url, json=payload, headers=headers)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload

    def get_policy_key(self, wallet_id: str, hash_format: bool = False) -> dict:
        """Get policy key for derivation index 0. hash_format = True returns a hash of the
        key instead."""
        self.logger.debug(f"Retrieving policy key for wallet {wallet_id}")
        url = f"{self.wallet_url}v2/wallets/{wallet_id}/policy-key?hash={hash_format}"
        self.logger.debug(f"URL: {url}")
        r = requests.get(url)
        if not r.ok:
            self.logger.error(f"Bad status code received: {r.status_code}, {r.text}")
            return {}
        payload = json.loads(r.text)
        self.logger.debug(r.text)
        return payload


class WalletCLI:
    """We recommend using the WalletHTTP class over this CLI class"""

    def __init__(
        self,
        path_to_cli,
        port=8090,
        network="--mainnet",
    ):
        self.cli = path_to_cli
        self.network = network
        self.port = port
        self.logger = logging.getLogger(__name__)

    def run_cli(self, cmd) -> tuple:
        # Execute the commands locally
        # For network instances use the HTTP class.
        cmd = f"{self.cli} {cmd}"
        result = subprocess.run(shlex.split(cmd), capture_output=True)
        stdout = result.stdout.decode().strip()
        stderr = result.stderr.decode().strip()
        self.logger.debug(f'CMD: "{cmd}"')
        self.logger.debug(f'stdout: "{stdout}"')
        self.logger.debug(f'stderr: "{stderr}"')
        ResultType = namedtuple("Result", "stdout, stderr")
        return ResultType(stdout, stderr)

    def recovery_phrase_generate(self, size: int = 24) -> str:
        """Generate a recovery or seed phrase (mnemonic)."""
        result = self.run_cli(f"recovery-phrase generate --size={size}")
        return result.stdout

    def create_wallet(
        self,
        name: str,
        recovery_phrase: str,
        passphrase: str,
        secondary_phrase: str = " ",
        address_pool_gap: int = 20,
    ) -> None:
        """Creates a new wallet with the provided recovery phrase and optional secondary phrase"""
        self.logger.debug(f"Running create wallet command...")
        child = pexpect.spawn(
            f"{self.cli} wallet create from-recovery-phrase {name} --port {self.port} --address-pool-gap {address_pool_gap}",
            timeout=2,
        )
        try:
            child.expect("Please enter the .* recovery phrase:")
            child.sendline(recovery_phrase)
            child.expect("Please enter a .* second factor:")
            child.sendline(secondary_phrase)
            child.expect("Please enter a passphrase:")
            child.sendline(passphrase)
            child.expect("Enter the passphrase a second time:")
            child.sendline(passphrase)
            child.expect("Ok.")
            self.logger.debug(f"Create wallet result: {child.after}")
        except:
            self.logger.error(f"Error creating wallet: {child}")

    def create_wallet_from_key(
        self,
        name: str,
        xpub_key: str,
        address_pool_gap: int = 20,
    ) -> dict:
        """Creates a new wallet with the provided account extended public key (public key + chain code)"""
        self.logger.debug(f"Running create wallet command...")
        res = self.run_cli(
            f"wallet create from-public-key {name} --address-pool-gap {address_pool_gap} {xpub_key}"
        )
        if len(res.stdout) > 0:
            wallet = json.loads(res.stdout)
            return wallet
        else:
            return {}

    def get_all_wallets(self) -> dict:
        """Get a list of all created wallets known to the wallet service.

        Returns
        ----------
        list
            List of dicts each representing the wallet info.
        """
        wallet_list = []
        res = self.run_cli("wallet list")
        if len(res.stdout) > 0:
            wallet_list = json.loads(res.stdout)
            return wallet_list
        else:
            return {}

    def get_wallet(self, wallet_id: str) -> dict:
        """Find the wallet specified by the ID.

        Parameters
        ----------
        wallet_id : str
            The wallet ID.
        """

        res = self.run_cli(f"wallet get --port={self.port} {wallet_id}")
        if "ok" in res.stderr.lower():
            return json.loads(res.stdout)
        return {}

    def get_wallet_by_name(self, name: str) -> dict:
        """Find the wallet from the supplied name (case insensitive).

        Parameters
        ----------
        name : str
            The arbitrary name of the wallet supplied during creation.
        """

        # First get a list of all wallets known to the local install.
        all_wallets = self.get_all_wallets()
        for wallet in all_wallets:
            if wallet.get("name").lower() == name.lower():
                return wallet
        return {}

    def delete_wallet(self, wallet_id: str) -> None:
        """Delete a wallet from cardano-wallet data by ID.

        Parameters
        ----------
        wallet_id : str
            The wallet ID.

        Raises
        ------
        WalletError
            If the wallet ID is not found.
        """
        res = self.run_cli(f"wallet delete --port {self.port} {wallet_id}")
        if len(res.stderr) > 3:  # stderr is "Ok." on success
            raise WalletError(res.stderr)

    def get_balance(self, wallet_id: str) -> float:
        """Get the wallet balance in ADA.

        Parameters
        ----------
        wallet_id : str
            The wallet ID.

        Returns
        ----------
        float
            The total wallet balance (including rewards) in ADA.
        """
        # TODO: Add asset balance
        wallet = self.get_wallet(wallet_id)
        bal = float(wallet.get("balance").get("total").get("quantity"))
        return bal / 1_000_000  # Return the value in units of ADA

    def get_utxo_stats(self, wallet_id: str) -> dict:
        """Get wallet's UTxO distribution statistics"""
        wallet = self.get_wallet(wallet_id)
        res = self.run_cli(f"wallet utxo --port {self.port} {wallet_id}")
        if res:
            return json.loads(res.stdout)

    def get_utxo_snapshot(self, wallet_id: str) -> dict:
        """Get wallet's UTxO snapshot"""
        wallet = self.get_wallet(wallet_id)
        res = self.run_cli(f"wallet utxo-snapshot --port {self.port} {wallet_id}")
        if res:
            return json.loads(res.stdout)


if __name__ == "__main__":
    # Not used as a script
    pass
