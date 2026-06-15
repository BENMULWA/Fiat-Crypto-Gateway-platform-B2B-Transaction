import json
import logging
import os
import shlex
import subprocess
import sys
from collections import namedtuple
from ctypes import Union
from datetime import datetime
from pathlib import Path
from typing import Tuple

import requests

# Cardano-Tools components
from . import utils

LATEST_SUPPORTED_NODE_VERSION = "1.32.1"


class NodeCLIError(Exception):
    pass


class NodeCLI:
    def __init__(
        self,
        binary_path,
        socket_path,
        working_dir,
        ttl_buffer=1000,
        network="--mainnet",
        era="--babbage-era",
    ):
        self.logger = logging.getLogger(__name__)

        # Debug flag -- may be set after object initialization.
        self.debug = False

        # Set the socket path, it must be set as an environment variable.
        # Set this first because its used during setup.
        self.socket = socket_path

        # Set the path to the CLI and verify it works. An exception will be
        # thrown if the command is not found.
        self.cli = binary_path
        self.check_node_version()

        # Set the working directory and make sure it exists.
        self.working_dir = Path(working_dir)
        self.working_dir.mkdir(parents=True, exist_ok=True)

        self.ttl_buffer = ttl_buffer
        self.network = network
        self.era = era
        self.protocol_parameters = None

        self.logger = logging.getLogger(__name__)

    def check_node_version(self):
        res = self.run_cli(f"{self.cli} --version")
        if res.stdout.split(" ")[1] != LATEST_SUPPORTED_NODE_VERSION:
            self.logger.warning(f"Unsupported cardano-node version.")

    def run_cli(self, cmd):
        os.environ["CARDANO_NODE_SOCKET_PATH"] = self.socket
        result = subprocess.run(shlex.split(cmd), capture_output=True)
        stdout = result.stdout.decode().strip()
        stderr = result.stderr.decode().strip()
        self.logger.debug(f'CMD: "{cmd}"')
        self.logger.debug(f'stdout: "{stdout}"')
        self.logger.debug(f'stderr: "{stderr}"')
        ResultType = namedtuple("Result", "stdout, stderr")
        return ResultType(stdout, stderr)

    def _load_text_file(self, fpath):
        text = open(fpath, "r").read()
        return text

    def _dump_text_file(self, fpath, datastr):
        with open(fpath, "w") as outfile:
            outfile.write(datastr)

    def _download_file(self, url, fpath):
        download = requests.get(url)
        with open(fpath, "wb") as download_file:
            download_file.write(download.content)

    def _cleanup_file(self, fpath):
        os.remove(fpath)

    def get_protocol_parameters(self):
        """Load the protocol parameters which are needed for creating
        transactions.
        """
        if self.protocol_parameters is None:
            stdout, stderr = self.run_cli(f"{self.cli} query protocol-parameters {self.network} ")
            self.protocol_parameters = json.loads(stdout)
        return self.protocol_parameters

    def save_protocol_parameters(self, outfile: str):
        """Saves the protocol parameters to the specified file"""
        self.run_cli(f"{self.cli} query protocol-parameters {self.network} --out-file {outfile}")

    def get_mempool_info(self) -> str:
        """Returns information about the node's mempool."""
        cmd = f"{self.cli} query tx-mempool info"
        result = self.run_cli(cmd)
        return result

    def get_mempool_next_tx(self) -> str:
        """Gets the next transaction to be processed by the node."""
        cmd = f"{self.cli} query tx-mempool next-tx"
        result = self.run_cli(cmd)
        return result

    def tx_in_mempool(self, transaction_id: str) -> bool:
        """Returns True if the provided transaction is in the node's mempool."""
        result = self.run_cli(f"{self.cli} query tx-mempool tx-exists {transaction_id}")
        # TODO: Parse output
        return result.stdout

    def get_min_utxo(self) -> int:
        """Get the minimum ADA only UTxO size."""
        return utils.minimum_utxo(self.get_protocol_parameters())

    def cli_tip_query(self):
        """Query the node for the current tip of the blockchain.
        Returns all the info from the query.
        """
        cmd = f"{self.cli} query tip {self.network}"
        result = self.run_cli(cmd)
        if "slot" not in result.stdout:
            raise NodeCLIError(result.stderr)
        vals = json.loads(result.stdout)
        return vals

    def get_sync_progress(self) -> float:
        """Query the node for the sync progress."""
        vals = self.cli_tip_query()
        return float(vals["syncProgress"])

    def get_epoch(self) -> int:
        """Query the node for the current epoch."""
        vals = self.cli_tip_query()
        if float(vals["syncProgress"]) != 100.0:
            self.logger.warning("Node not fully synced!")
        return vals["epoch"]

    def get_slot(self) -> int:
        """Query the node for the current slot."""
        vals = self.cli_tip_query()
        if float(vals["syncProgress"]) != 100.0:
            self.logger.warning("Node not fully synced!")
        return vals["slot"]

    def get_era(self) -> int:
        """Query the node for the current era."""
        vals = self.cli_tip_query()
        if float(vals["syncProgress"]) != 100.0:
            self.logger.warning("Node not fully synced!")
        return vals["era"]

    def get_tip(self) -> int:
        """Query the node for the current tip of the blockchain."""
        vals = self.cli_tip_query()
        if float(vals["syncProgress"]) != 100.0:
            self.logger.warning("Node not fully synced!")
        return vals["slot"]

    def make_address(self, name, folder=None) -> str:
        """Create an address and the corresponding payment and staking keys."""
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        payment_vkey = folder / (name + ".vkey")
        payment_skey = folder / (name + ".skey")
        stake_vkey = folder / (name + "_stake.vkey")
        stake_skey = folder / (name + "_stake.skey")
        payment_addr = folder / (name + ".addr")
        stake_addr = folder / (name + "_stake.addr")

        # Generate payment key pair.
        self.run_cli(
            f"{self.cli} address key-gen "
            f"--verification-key-file {payment_vkey} "
            f"--signing-key-file {payment_skey}"
        )

        # Generate stake key pair.
        self.run_cli(
            f"{self.cli} stake-address key-gen "
            f"--verification-key-file {stake_vkey} "
            f"--signing-key-file {stake_skey}"
        )

        # Create the payment address.
        self.run_cli(
            f"{self.cli} address build "
            f"--payment-verification-key-file {payment_vkey} "
            f"--stake-verification-key-file {stake_vkey} "
            f"--out-file {payment_addr} {self.network}"
        )

        # Create the staking address.
        self.run_cli(
            f"{self.cli} stake-address build "
            f"--stake-verification-key-file {stake_vkey} "
            f"--out-file {stake_addr} {self.network}"
        )

        # Read the file and return the payment address.
        addr = self._load_text_file(payment_addr).strip()
        return addr

    def get_key_hash(self, vkey_path) -> str:
        """Generate a public key hash from a verification key file.

        Parameters
        ----------
        vkey_path : str, Path
            Path to the verification key file.

        Returns
        -------
        str
            The key hash.
        """
        result = self.run_cli(
            f"{self.cli} address key-hash " f"--payment-verification-key-file {vkey_path}"
        )
        return result.stdout

    def get_utxos(self, addr, filter=None) -> list:
        """Query the list of UTXOs for a given address and parse the output.
        The returned data is formatted as a list of dict objects.

        Parameters
        ----------
        addr : str
            Address for which to find the UTXOs.
        filter : str, optional
            Filter the UTXOs based on a token ID. If "Lovelace" is passed to
            the filter, UTXOs containing ONLY lovelace will be returned.

        Returns
        -------
        list
            List of UTXOs parsed into dictionary objects.
        """

        # Query the UTXOs for the given address (this will not get everything
        # for a given wallet that contains multiple addresses.)
        result = self.run_cli(f"{self.cli} query utxo --address {addr} {self.network}")
        raw_utxos = result.stdout.split("\n")[2:]

        # Parse the UTXOs into a list of dict objects
        utxos = []
        for utxo_line in raw_utxos:
            vals = utxo_line.split()
            utxo_dict = {
                "TxHash": vals[0],
                "TxIx": vals[1],
                "Lovelace": vals[2],
            }

            # Extra tokens will be separated by a "+" sign.
            extra = [i for i, j in enumerate(vals) if j == "+"]
            for i in extra:
                if "TxOutDatum" in vals[i + 1]:
                    continue
                asset = vals[i + 2]
                amt = vals[i + 1]
                if asset in utxo_dict:
                    utxo_dict[asset] += amt
                else:
                    utxo_dict[asset] = amt
            utxos.append(utxo_dict)

        # Filter utxos
        if filter is not None:
            if filter == "Lovelace":
                utxos = [utxo for utxo in utxos if filter in utxo and len(utxo.keys()) == 3]
            else:
                utxos = [utxo for utxo in utxos if filter in utxo]

        return utxos

    def query_balance(self, addr) -> int:
        """Query an address balance in lovelace."""
        total = 0
        utxos = self.get_utxos(addr)
        for utxo in utxos:
            total += int(utxo["Lovelace"])
        return total

    def calc_min_fee(
        self,
        tx_draft,
        tx_in_count,
        tx_out_count,
        witness_count,
        byron_witness_count=0,
    ) -> int:
        """Calculate the minimum fee in lovelaces for the transaction.

        Parameters
        ----------
        tx_draft : str, Path
            Path to draft transaction file.
        tx_in_count : int
            The number of UTXOs being spent.
        tx_out_count : int
            The number of output UTXOs.
        witness_count : int
            The number of transaction signing keys.
        byron_witness_count : int, optional
            Number of Byron witnesses (defaults to 0).

        Returns
        -------
        int
            The minimum fee in lovelaces.
        """
        params_filepath = os.path.join(self.working_dir, "params.json")
        self.save_protocol_parameters(params_filepath)
        result = self.run_cli(
            f"{self.cli} transaction calculate-min-fee "
            f"--tx-body-file {tx_draft} "
            f"--tx-in-count {tx_in_count} "
            f"--tx-out-count {tx_out_count} "
            f"--witness-count {witness_count} "
            f"--byron-witness-count {byron_witness_count} "
            f"{self.network} --protocol-params-file {params_filepath}"
        )
        min_fee = int(result.stdout.split()[0])
        return min_fee

    def send_payment(self, amt, to_addr, from_addr, key_file, offline=False, cleanup=True):
        """Send ADA from one address to another.

        Parameters
        ----------
        amt : float
            Amount of ADA to send (before fee).
        to_addr : str
            Address to send the ADA to.
        from_addr : str
            Address to send the ADA from.
        key_file : str or Path
            Path to the send address signing key file.
        offline: bool, optional
            Flag to indicate if the transactions is being generated offline.
            If true (defaults to false), the transaction file is signed but
            not sent.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """
        payment = amt * 1_000_000  # ADA to Lovelaces

        # Build the transaction
        tx_raw_file = self.build_raw_transaction(
            from_addr,
            witness_count=1,
            receive_addrs=[to_addr],
            payments=[payment],
            certs=None,
            deposits=0,
            folder=None,
            cleanup=cleanup,
        )

        # Sign the transaction with the signing key
        tx_signed_file = self.sign_transaction(tx_raw_file, [key_file])

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_raw_file)

        # Submit the transaction
        if not offline:
            self.submit_transaction(tx_signed_file, cleanup)
        else:
            self.logger.info(f"Signed transaction file saved to: {tx_signed_file}")

    def register_stake_address(
        self,
        addr,
        stake_vkey_file,
        stake_skey_file,
        pmt_skey_file,
        offline=False,
        cleanup=True,
    ):
        """Register a stake address in the blockchain.

        Parameters
        ----------
        addr : str
            Address of the staking key being registered.
        stake_vkey_file : str or Path
            Path to the staking verification key.
        stake_skey_file : str or Path
            Path to the staking signing key.
        pmt_skey_file : str or Path
            Path to the payment signing key.
        offline: bool, optional
            Flag to indicate if the transactions is being generated offline.
            If true (defaults to false), the transaction file is signed but
            not sent.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # Build a transaction name
        tx_name = datetime.now().strftime("reg_stake_key_%Y-%m-%d_%Hh%Mm%Ss")

        # Create a registration certificate
        key_file_path = Path(stake_vkey_file)
        stake_cert_path = key_file_path.parent / (key_file_path.stem + ".cert")
        self.run_cli(
            f"{self.cli} stake-address registration-certificate "
            f"--stake-verification-key-file {stake_vkey_file} "
            f"--out-file {stake_cert_path}"
        )

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Get a list of UTXOs and sort them in decending order by value.
        utxos = self.get_utxos(addr)
        if len(utxos) < 1:
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. "
                f"Account {addr} cannot pay transaction costs because "
                "it does not contain any ADA."
            )
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)

        # Ensure the parameters file exists
        self.get_protocol_parameters()

        # Iterate through the UTXOs until we have enough funds to cover the
        # transaction. Also, create the tx_in string for the transaction.
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        utxo_total = 0
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f" --tx-in {utxo['TxHash']}#{utxo['TxIx']}"

            # Build a transaction draft
            self.run_cli(
                f"{self.cli} transaction build-raw{tx_in_str} "
                f"--tx-out {addr}+0 --ttl 0 --fee 0 "
                f"--certificate-file {stake_cert_path} "
                f"--out-file {tx_draft_file}"
            )

            # Calculate the minimum fee
            min_fee = self.calc_min_fee(tx_draft_file, utxo_count, tx_out_count=1, witness_count=2)

            # TX cost
            cost = min_fee + self.get_protocol_parameters.get("stakeAddressDeposit")
            if utxo_total > cost:
                break

        if utxo_total < cost:
            cost_ada = cost / 1_000_000
            utxo_total_ada = utxo_total / 1_000_000
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. "
                f"Account {addr} cannot pay transaction costs of {cost_ada} "
                f"ADA because it only contains {utxo_total_ada} ADA."
            )

        # Build the transaction.
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        self.run_cli(
            f"{self.cli} transaction build-raw{tx_in_str} "
            f"--tx-out {addr}+{utxo_total - cost} "
            f"--ttl {ttl} --fee {min_fee} "
            f"--certificate-file {stake_cert_path} "
            f"--out-file {tx_raw_file}"
        )

        # Sign the transaction with both the payment and stake keys.
        tx_signed_file = Path(self.working_dir) / (tx_name + ".signed")
        self.run_cli(
            f"{self.cli} transaction sign "
            f"--tx-body-file {tx_raw_file} --signing-key-file {pmt_skey_file} "
            f"--signing-key-file {stake_skey_file} {self.network} "
            f"--out-file {tx_signed_file}"
        )

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)
            self._cleanup_file(tx_raw_file)

        # Submit the transaction
        if not offline:
            self.submit_transaction(tx_signed_file, cleanup)
        else:
            self.logger.info(f"Signed transaction file saved to: {tx_signed_file}")

    def generate_kes_keys(self, pool_name="pool", folder=None) -> Tuple[str, str]:
        """Generate a new set of KES keys for a stake pool.

        KES == Key Evolving Signature

        Parameters
        ----------
        pool_name : str
            Pool name for file/certificate naming.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.

        Returns
        _______
        (str, str)
            Paths to the new verification and signing KES key files.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Generate the KES Key pair
        kes_vkey = folder / (pool_name + "_kes.vkey")
        kes_skey = folder / (pool_name + "_kes.skey")
        self.run_cli(
            f"{self.cli} node key-gen-KES "
            f"--verification-key-file {kes_vkey} "
            f"--signing-key-file {kes_skey}"
        )

        return (kes_vkey, kes_skey)

    def create_block_producing_keys(self, genesis_file, pool_name="pool", folder=None):
        """Create keys for a block-producing node.
        WARNING: You may want to use your local machine for this process
        (assuming you have cardano-node and cardano-cli on it). Make sure you
        are not online until you have put your cold keys in a secure storage
        and deleted the files from you local machine.

        The block-producing node or pool node needs:
            Cold key pair,
            VRF Key pair,
            KES Key pair,
            Operational Certificate

        Parameters
        ----------
        genesis_file : str or Path
            Path to the genesis file.
        pool_name : str
            Pool name for file/certificate naming.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Generate Cold Keys and a Cold_counter
        cold_vkey = folder / (pool_name + "_cold.vkey")
        cold_skey = folder / (pool_name + "_cold.skey")
        cold_counter = folder / (pool_name + "_cold.counter")
        self.run_cli(
            f"{self.cli} node key-gen "
            f"--cold-verification-key-file {cold_vkey} "
            f"--cold-signing-key-file {cold_skey} "
            f"--operational-certificate-issue-counter-file {cold_counter}"
        )

        # Generate VRF Key pair
        vrf_vkey = folder / (pool_name + "_vrf.vkey")
        vrf_skey = folder / (pool_name + "_vrf.skey")
        self.run_cli(
            f"{self.cli} node key-gen-VRF "
            f"--verification-key-file {vrf_vkey} "
            f"--signing-key-file {vrf_skey}"
        )

        # Generate the KES Key pair
        kes_vkey, kes_skey = self.generate_kes_keys(pool_name, folder)

        # Get the network genesis parameters
        json_data = self._load_text_file(genesis_file)
        genesis_parameters = json.loads(json_data)

        # Generate the Operational Certificate/
        cert_file = folder / (pool_name + ".cert")
        slots_kes_period = genesis_parameters["slotsPerKESPeriod"]
        tip = self.get_tip()
        kes_period = tip // slots_kes_period  # Integer division
        self.run_cli(
            f"{self.cli} node issue-op-cert "
            f"--kes-verification-key-file {kes_vkey} "
            f"--cold-signing-key-file {cold_skey} "
            f"--operational-certificate-issue-counter {cold_counter} "
            f"--kes-period {kes_period} --out-file {cert_file}"
        )

        # Get the pool ID and return it.
        result = self.run_cli(
            f"{self.cli} stake-pool id " f"--cold-verification-key-file {cold_vkey}"
        )
        pool_id = result.stdout
        self._dump_text_file(folder / (pool_name + ".id"), pool_id)

        return pool_id  # Return the pool id after first saving it to a file.

    def update_kes_keys(
        self,
        genesis_file,
        cold_skey,
        cold_counter,
        pool_name="pool",
        folder=None,
    ):
        """Update KES keys for an existing stake pool.

        Parameters
        ----------
        genesis_file : str or Path
            Path to the genesis file.
        cold_skey : str or Path
            Path to the pool's cold signing key.
        cold_counter : str or Path
            Path to the pool's cold counter file.
        pool_name : str
            Pool name for file/certificate naming.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Generate the new KES key pair
        kes_vkey, kes_skey = self.generate_kes_keys(pool_name, folder)

        # Generate the new pool operation certificate
        # Get the network genesis parameters
        json_data = self._load_text_file(genesis_file)
        genesis_parameters = json.loads(json_data)

        # Generate the Operational Certificate
        cert_file = folder / (pool_name + ".cert")
        slots_kes_period = genesis_parameters["slotsPerKESPeriod"]
        tip = self.get_tip()
        kes_period = tip // slots_kes_period  # Integer division
        result = self.run_cli(
            f"{self.cli} node issue-op-cert "
            f"--kes-verification-key-file {kes_vkey} "
            f"--cold-signing-key-file {cold_skey} "
            f"--operational-certificate-issue-counter {cold_counter} "
            f"--kes-period {kes_period} --out-file {cert_file}"
        )

        if result.stderr:
            raise NodeCLIError(f"Unable to rotate KES keys: {result.stderr}")

    def create_metadata_file(self, pool_metadata, folder=None) -> str:
        """Create a JSON file with the pool metadata and return the file hash."""

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Create a JSON file with the pool metadata and return the file hash.
        ticker = pool_metadata["ticker"]
        metadata_file_path = folder / f"{ticker}_metadata.json"
        self._dump_text_file(metadata_file_path, json.dumps(pool_metadata).strip())
        result = self.run_cli(
            f"{self.cli} stake-pool metadata-hash " f"--pool-metadata-file {metadata_file_path}"
        )
        metadata_hash = result.stdout.strip()
        return metadata_hash

    def generate_stake_pool_cert(
        self,
        pool_name,
        pool_pledge,
        pool_cost,
        pool_margin,
        pool_cold_vkey,
        pool_vrf_key,
        pool_reward_vkey,
        owner_stake_vkeys,
        pool_relays=None,
        pool_metadata_url=None,
        pool_metadata_hash=None,
        folder=None,
    ) -> str:
        """Generate a stake pool certificate.

        This function generates a stake pool registration certificate. It can
        be used without connection to a running node for offline applications.

        Parameters
        ----------
        pool_name : str
            Pool name for file/certificate naming.
        pool_metadata : dict
            Dictionary of stake pool metadata to be converted to json.
        pool_pledge : int
            Pool pledge amount in lovelace.
        pool_cost : int
            Pool cost (fixed fee per epoch) in lovelace.
        pool_margin : float
            Pool margin (variable fee) as a percentage.
        pool_cold_vkey : str or Path
            Path to the pool's cold verification key.
        pool_vrf_key : str or Path
            Path to the pool's verification key.
        pool_reward_vkey : str or Path
            Path to the staking verification key that will receive pool
            rewards.
        owner_stake_vkeys : list
            List of owner stake verification keys (paths) responsible for the
            pledge.
        pool_relays: list, optional,
            List of dictionaries each representing a pool relay. The
            dictionaries have three required keys:
                "port" specifying the relay's port number,
                "host" specifying the host name (IP, DNS, etc.),
                "host-type" specifying the type of data in the "host" key.
        pool_metadata_url : str, optional
            URL to the pool's metadata JSON file.
        pool_metadata_hash : str, optional
            Optionally specify the hash of the metadata JSON file. If this is
            not specified and the pool_metadata_hash is, then the code will
            download the file from the URL and compute the hash.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.

        Returns
        -------
        str
            The path to the stake pool registration certificate file.
        """
        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Get the hash of the JSON file if the URL is provided and the hash is
        # not specified.
        metadata_args = ""
        if pool_metadata_url is not None:
            if pool_metadata_hash is None:
                metadata_file = folder / "metadata_file_download.json"
                self._download_file(pool_metadata_url, metadata_file)
                result = self.run_cli(
                    f"{self.cli} stake-pool metadata-hash " f"--pool-metadata-file {metadata_file}"
                )
                pool_metadata_hash = result.stdout.strip()

            # Create the arg string for the pool cert.
            metadata_args = (
                f"--metadata-url {pool_metadata_url} " f"--metadata-hash {pool_metadata_hash}"
            )

        # Create the relay arg string. Basically, we need a port and host arg
        # but there can be different forms of the host argument. See the
        # caradno-cli documentation. The simpliest way I could figure was to
        # use a list of dictionaries where each dict represents a relay.
        relay_args = ""
        for relay in pool_relays:
            if "ipv4" in relay["host-type"]:
                host_arg = f"--pool-relay-ipv4 {relay['host']}"
            elif "ipv6" in relay["host-type"]:
                host_arg = f"--pool-relay-ipv4 {relay['host']}"
            elif "single" in relay["host-type"]:
                host_arg = f"--single-host-pool-relay {relay['host']}"
            elif "multi" in relay["host-type"]:
                relay_args += f"--multi-host-pool-relay {relay['host']}"
                continue  # No port info for this case
            else:
                continue  # Skip if invalid host type
            port_arg = f"--pool-relay-port {relay['port']}"
            relay_args += f"{host_arg} {port_arg} "

        # Create the argument string for the list of owner verification keys.
        owner_vkey_args = ""
        for key_path in owner_stake_vkeys:
            arg = f"--pool-owner-stake-verification-key-file {key_path} "
            owner_vkey_args += arg

        # Generate Stake pool registration certificate
        ts = datetime.now().strftime("tx_%Y-%m-%d_%Hh%Mm%Ss")
        pool_cert_path = folder / (pool_name + "_registration_" + ts + ".cert")
        result = self.run_cli(
            f"{self.cli} stake-pool registration-certificate "
            f"--cold-verification-key-file {pool_cold_vkey} "
            f"--vrf-verification-key-file {pool_vrf_key} "
            f"--pool-pledge {pool_pledge} "
            f"--pool-cost {pool_cost} "
            f"--pool-margin {pool_margin/100} "
            f"--pool-reward-account-verification-key-file {pool_reward_vkey} "
            f"{owner_vkey_args} {relay_args} {metadata_args} "
            f"{self.network} --out-file {pool_cert_path}"
        )
        if result.stderr:
            raise NodeCLIError(f"Unable to create certificate: {result.stderr}")

        # Return the path to the generated pool cert
        return pool_cert_path

    def generate_delegation_cert(self, owner_stake_vkeys, pool_cold_vkey, folder=None):
        """Generate a delegation certificate for pledging.

        Parameters
        ----------
        owner_stake_vkeys : list
            List of owner stake verification keys (paths) responsible for the
            pledge.
        pool_cold_vkey : str or Path
            Path to the pool's cold verification key.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Generate delegation certificate (pledge from each owner)
        ts = datetime.now().strftime("tx_%Y-%m-%d_%Hh%Mm%Ss")
        certs = []
        for key_path in owner_stake_vkeys:
            key_path = Path(key_path)
            cert_path = key_path.parent / (key_path.stem + "_delegation_" + ts + ".cert")
            self.run_cli(
                f"{self.cli} stake-address delegation-certificate "
                f"--stake-verification-key-file {key_path} "
                f"--cold-verification-key-file {pool_cold_vkey} "
                f"--out-file {cert_path}"
            )
            certs.append(cert_path)

        # Return a list of certificate files
        return certs

    def build_raw_transaction(
        self,
        payment_addr,
        witness_count=1,
        receive_addrs=None,
        payments=None,
        certs=None,
        deposits=0,
        folder=None,
        cleanup=True,
    ) -> str:
        """Build a raw (unsigned) transaction.

        Requires a running and synced node.

        Parameters
        ----------
        payment_addr : str
            Address to pay the fees, deposites, and payments.
        receive_addrs : list, optional
            Address to receive payment.
        payments: list, optional
            Payments (lovelaces) corresponding to the list of receive addresses.
        certs: list, optional
            List of certificate files to include in the transaction.
        deposits: int, optional
            Deposits
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).

        Returns
        -------
        str
            Resturns the path to the raw transaction file.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Get a list of certificate arguments
        cert_args = ""
        if certs:
            for cert_path in certs:
                cert_args += f"--certificate-file {cert_path} "

        # Sume the total payments
        total_payments = 0
        if payments:
            total_payments = sum(payments)

        # Get a list of payment args
        pymt_args_zero = ""
        pymt_args = ""
        if receive_addrs:
            for addr, amt in zip(receive_addrs, payments):
                pymt_args_zero += f"--tx-out {addr}+0 "
                pymt_args += f"--tx-out {addr}+{amt:.0f} "

        # Get a list of UTXOs and sort them in decending order by value.
        utxos = self.get_utxos(payment_addr, filter="Lovelace")
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Ensure the parameters file exists
        min_utxo = self.get_min_utxo()

        # Iterate through the UTXOs until we have enough funds to cover the
        # transaction. Also, create the tx_in string for the transaction.
        tx_name = datetime.now().strftime("tx_%Y-%m-%d_%Hh%Mm%Ss")
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        lovelaces_out = sys.maxsize  # must be larger than zero
        utxo_total = 0
        min_fee = 1  # make this start greater than utxo_total
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f"--tx-in {utxo['TxHash']}#{utxo['TxIx']} "

            # Build a transaction draft
            self.run_cli(
                f"{self.cli} transaction build-raw {self.era} {tx_in_str}"
                f"--tx-out {payment_addr}+0 {pymt_args_zero} --ttl 0 --fee 0 "
                f"--out-file {tx_draft_file} {cert_args}"
            )

            # Calculate the minimum fee
            min_fee = self.calc_min_fee(
                tx_draft_file,
                utxo_count,
                tx_out_count=1,
                witness_count=witness_count,
            )

            # If we have enough Lovelaces to cover the transaction can stop
            # iterating through the UTXOs.
            lovelaces_out = min_fee + deposits + total_payments
            utxo_amt = utxo_total - lovelaces_out
            if utxo_total > lovelaces_out and (utxo_amt > min_utxo or utxo_amt == 0):
                break

        # Handle the error case where there is not enough inputs for the output
        cost_ada = lovelaces_out / 1_000_000
        utxo_total_ada = utxo_total / 1_000_000
        if utxo_total < lovelaces_out:
            # This is the case where the sending wallet has no UTXOs to spend.
            # The above for loop didn't run at all which is why the
            # lovelaces_out value is still sys.maxsize.
            if lovelaces_out == sys.maxsize:
                raise NodeCLIError(
                    f"Transaction failed due to insufficient funds. Account "
                    f"{payment_addr} is empty."
                )
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. Account "
                f"{payment_addr} cannot pay transaction costs of {cost_ada} "
                f"ADA because it only contains {utxo_total_ada} ADA."
            )

        # Setup the new UTXO
        utxo_str = ""
        if utxo_amt == 0:
            # The transaction is emptying the account. No UTXO.
            pass
        elif utxo_amt < min_utxo:
            # Verify that the UTXO is larger than the minimum.
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. Account "
                f"{payment_addr} cannot pay transaction costs of {cost_ada} "
                f"ADA because it only contains {utxo_total_ada} ADA "
                f"resulting in an UTxO of {utxo_total_ada - cost_ada} ADA "
                f"which is less than the minimum of {min_utxo / 1_000_000}."
            )
        else:
            utxo_str = f"--tx-out {payment_addr}+{utxo_amt}"

        # Build the transaction to the blockchain.
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        self.run_cli(
            f"{self.cli} transaction build-raw {self.era} {tx_in_str} "
            f"{utxo_str} {pymt_args} --ttl {ttl} --fee {min_fee} "
            f"--out-file {tx_raw_file} {cert_args}"
        )

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)

        # Return the path to the raw transaction file.
        return tx_raw_file

    def build_multisignature_scripts(
        self,
        script_name,
        key_hashes,
        sig_type,
        required=None,
        start_slot=None,
        end_slot=None,
        folder=None,
    ) -> str:
        """Helper function for building multi-signature scripts.

        This script is not required as the multi-signature scripts may be created by hand.

        Parameters
        ----------
        name : str
            Name of the script
        key_hashes : list
            List of key hashes (use get_key_hash)
        sig_type : str
            Signature type (all, any, atLeast)
        required : int, optional
            Number of required signatures (used with type="atLeast")
        start_slot : int, optional
            Lower bound on slots where minting is allowed
        end_slot : int, optional
            Upper bound on slots where minting is allowed

        Returns
        -------
        str
            Path to the multi-signature script file.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Build the list of signature hashes
        script = {"scripts": [{"keyHash": h, "type": "sig"} for h in key_hashes]}

        # Determine the type. Default to all
        sig_type = sig_type.lower()
        if sig_type == "any":
            script["type"] = "any"
        elif sig_type == "atleast" and required is not None:
            script["type"] = "atLeast"
            script["required"] = int(required)
            if script["required"] < 1 or script["required"] >= len(key_hashes):
                raise NodeCLIError("Invalid number of required signatures.")
        else:
            script["type"] = "all"

        # Add bounds
        if start_slot is not None:
            script["scripts"].append({"slot": start_slot, "type": "after"})
        if end_slot is not None:
            script["scripts"].append({"slot": end_slot, "type": "before"})

        # Write the script file
        file_path = Path(folder) / (script_name + ".json")
        with open(file_path, "w") as outfile:
            json.dump(script, outfile, indent=4)

        return file_path

    def witness_transaction(self, tx_file, witnesses) -> str:
        """Sign a transaction file with witness file(s).

        Parameters
        ----------
        tx_file : str or Path
            Path to the transaction file to be signed.
        witnesses : list
            List of paths (str or Path) to the witness files.

        Returns
        -------
        str
            Path to the signed transaction file.
        """

        # Generate a list of witness args.
        witness_args = ""
        for witness in witnesses:
            witness_args += f"--witness-file {witness} "

        # Sign the transaction with the signing key
        tx_name = Path(tx_file).stem
        tx_signed_file = tx_name + ".signed"
        self.run_cli(
            f"{self.cli} transaction sign-witness "
            f"--tx-body-file {tx_file} {witness_args}"
            f"--out-file {tx_signed_file}"
        )

        # Return the path to the signed file for downstream use.
        return tx_signed_file

    def sign_transaction(self, tx_file, skeys) -> str:
        """Sign a transaction file with a signing key.

        Parameters
        ----------
        tx_file : str or Path
            Path to the transaction file to be signed.
        skeys : list
            List of paths (str or Path) to the signing key files.

        Returns
        -------
        str
            Path to the signed transaction file.
        """

        # Generate a list of signing key args.
        signing_key_args = ""
        for key_path in skeys:
            signing_key_args += f"--signing-key-file {key_path} "

        # Sign the transaction with the signing key
        tx_name = Path(tx_file).stem
        tx_signed_file = tx_name + ".signed"
        result = self.run_cli(
            f"{self.cli} transaction sign "
            f"--tx-body-file {tx_file} {signing_key_args} "
            f"{self.network} --out-file {tx_signed_file}"
        )

        if result.stderr:
            raise NodeCLIError(f"Unable to sign transaction: {result.stderr}")

        # Return the path to the signed file for downstream use.
        return tx_signed_file

    def submit_transaction(self, signed_tx_file, cleanup=False) -> str:
        """Submit a transaction to the blockchain. This function is separate to
        enable the submissions of transactions signed by offline keys.

        Parameters
        ----------
        signed_tx_file : str or Path
            Path to the signed transaction file ready for submission.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to false).

        Returns
        -------
        str
            The transaction ID.
        """

        # Submit the transaction
        result = self.run_cli(
            f"{self.cli} transaction submit " f"--tx-file {signed_tx_file} {self.network}"
        )

        if result.stderr:
            raise NodeCLIError(f"Unable to submit transaction: {result.stderr}")

        # Get the transaction ID
        result = self.run_cli(f"{self.cli} transaction txid --tx-file {signed_tx_file}")
        txid = result.stdout.strip()

        # Delete the transaction files if specified.
        if cleanup:
            self._cleanup_file(signed_tx_file)

        return txid

    def register_stake_pool(
        self,
        pool_name,
        pool_pledge,
        pool_cost,
        pool_margin,
        pool_cold_vkey,
        pool_cold_skey,
        pool_vrf_key,
        pool_reward_vkey,
        owner_stake_vkeys,
        owner_stake_skeys,
        payment_addr,
        payment_skey,
        genesis_file,
        pool_relays=None,
        pool_metadata_url=None,
        pool_metadata_hash=None,
        folder=None,
        offline=False,
        cleanup=True,
    ):
        """Register a stake pool on the blockchain.

        Parameters
        ----------
        pool_name : str
            Pool name for file/certificate naming.
        pool_metadata : dict
            Dictionary of stake pool metadata to be converted to json.
        pool_pledge : int
            Pool pledge amount in lovelace.
        pool_cost : int
            Pool cost (fixed fee per epoch) in lovelace.
        pool_margin : float
            Pool margin (variable fee) as a percentage.
        pool_cold_vkey : str or Path
            Path to the pool's cold verification key.
        pool_cold_skey : str or Path
            Path to the pool's cold signing key.
        pool_vrf_key : str or Path
            Path to the pool's verification key.
        pool_reward_vkey : str or Path
            Path to the staking verification key that will receive pool
            rewards.
        owner_stake_vkeys : list
            List of owner stake verification keys (paths) responsible for the
            pledge.
        owner_stake_skeys : list
            List of owner stake signing keys (paths) responsible for the
            pledge.
        payment_addr : str
            Address responsible for paying the pool registration and
            transaction fees.
        payment_skey : str or Path
            Signing key for the address responsible for paying the pool
            registration and transaction fees.
        genesis_file : str or Path
            Path to the genesis file.
        pool_relays: list, optional,
            List of dictionaries each representing a pool relay. The
            dictionaries have three required keys:
                "port" specifying the relay's port number,
                "host" specifying the host name (IP, DNS, etc.),
                "host-type" specifying the type of data in the "host" key.
        pool_metadata_url : str, optional
            URL to the pool's metadata JSON file.
        pool_metadata_hash : str, optional
            Optionally specify the hash of the metadata JSON file. If this is
            not specified and the pool_metadata_hash is, then the code will
            download the file from the URL and compute the hash.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.
        offline: bool, optional
            Flag to indicate if the transactions is being generated offline.
            If true (defaults to false), the transaction file is signed but
            not sent.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        pool_cert_path = self.generate_stake_pool_cert(
            pool_name,
            pool_pledge,
            pool_cost,
            pool_margin,
            pool_cold_vkey,
            pool_vrf_key,
            pool_reward_vkey,
            owner_stake_vkeys,
            pool_relays=pool_relays,
            pool_metadata_url=pool_metadata_url,
            pool_metadata_hash=pool_metadata_hash,
            folder=folder,
        )

        # Generate delegation certificates (pledge from each owner)
        del_certs = self.generate_delegation_cert(owner_stake_vkeys, pool_cold_vkey, folder=folder)
        del_cert_args = ""
        for cert_path in del_certs:
            del_cert_args += f"--certificate-file {cert_path} "

        # Generate a list of owner signing key args.
        signing_key_args = ""
        for key_path in owner_stake_skeys:
            signing_key_args += f"--signing-key-file {key_path} "

        # Get the pool deposit from the network genesis parameters.
        json_data = self._load_text_file(genesis_file)
        pool_deposit = json.loads(json_data)["protocolParams"]["poolDeposit"]

        # Get a list of UTXOs and sort them in decending order by value.
        utxos = self.get_utxos(payment_addr)
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Ensure the parameters file exists
        self.get_protocol_parameters()

        # Iterate through the UTXOs until we have enough funds to cover the
        # transaction. Also, create the tx_in string for the transaction.
        tx_name = datetime.now().strftime("reg_pool_%Y-%m-%d_%Hh%Mm%Ss")
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        utxo_total = 0
        min_fee = 1  # make this start greater than utxo_total
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f" --tx-in {utxo['TxHash']}#{utxo['TxIx']}"

            # Build a transaction draft
            self.run_cli(
                f"{self.cli} transaction build-raw{tx_in_str} "
                f"--tx-out {payment_addr}+0 --ttl 0 --fee 0 "
                f"--out-file {tx_draft_file} "
                f"--certificate-file {pool_cert_path} {del_cert_args}"
            )

            # Calculate the minimum fee
            nwit = len(owner_stake_skeys) + 2
            min_fee = self.calc_min_fee(
                tx_draft_file, utxo_count, tx_out_count=1, witness_count=nwit
            )

            if utxo_total > (min_fee + pool_deposit + 10):
                break

        if utxo_total < (min_fee + pool_deposit):
            cost_ada = (min_fee + pool_deposit) / 1_000_000
            utxo_total_ada = utxo_total / 1_000_000
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. Account "
                f"{payment_addr} cannot pay transaction costs of {cost_ada} "
                f"lovelaces because it only contains {utxo_total_ada} ADA."
            )

        # Build the transaction to submit the pool certificate and delegation
        # certificate(s) to the blockchain.
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        self.run_cli(
            f"{self.cli} transaction build-raw{tx_in_str} "
            f"--tx-out {payment_addr}+{utxo_total - min_fee - pool_deposit} "
            f"--ttl {ttl} --fee {min_fee} --out-file {tx_raw_file} "
            f"--certificate-file {pool_cert_path} {del_cert_args}"
        )

        # Sign the transaction with both the payment and stake keys.
        tx_signed_file = Path(self.working_dir) / (tx_name + ".signed")
        self.run_cli(
            f"{self.cli} transaction sign "
            f"--tx-body-file {tx_raw_file} --signing-key-file {payment_skey} "
            f"{signing_key_args} --signing-key-file {pool_cold_skey} "
            f"{self.network} --out-file {tx_signed_file}"
        )

        # Delete the transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)
            self._cleanup_file(tx_raw_file)

        # Submit the transaction
        if not offline:
            self.submit_transaction(tx_signed_file, cleanup)
        else:
            self.logger.info(f"Signed transaction file saved to: {tx_signed_file}")

    def update_stake_pool_registration(
        self,
        pool_name,
        pool_pledge,
        pool_cost,
        pool_margin,
        pool_cold_vkey,
        pool_cold_skey,
        pool_vrf_key,
        pool_reward_vkey,
        owner_stake_vkeys,
        owner_stake_skeys,
        payment_addr,
        payment_skey,
        genesis_file,
        pool_relays=None,
        pool_metadata_url=None,
        pool_metadata_hash=None,
        folder=None,
        offline=False,
        cleanup=True,
    ):
        """Update an existing stake pool registration on the blockchain.

        Parameters
        ----------
        pool_name : str
            Pool name for file/certificate naming.
        pool_metadata : dict
            Dictionary of stake pool metadata to be converted to json.
        pool_pledge : int
            Pool pledge amount in lovelace.
        pool_cost : int
            Pool cost (fixed fee per epoch) in lovelace.
        pool_margin : float
            Pool margin (variable fee) as a percentage.
        pool_cold_vkey : str or Path
            Path to the pool's cold verification key.
        pool_cold_skey : str or Path
            Path to the pool's cold signing key.
        pool_vrf_key : str or Path
            Path to the pool's verification key.
        pool_reward_vkey : str or Path
            Path to the staking verification key that will receive pool
            rewards.
        owner_stake_vkeys : list
            List of owner stake verification keys (paths) responsible for the
            pledge.
        owner_stake_skeys : list
            List of owner stake signing keys (paths) responsible for the
            pledge.
        payment_addr : str
            Address responsible for paying the pool registration and
            transaction fees.
        payment_skey : str or Path
            Signing key for the address responsible for paying the pool
            registration and transaction fees.
        genesis_file : str or Path
            Path to the genesis file.
        pool_relays: list, optional,
            List of dictionaries each representing a pool relay. The
            dictionaries have three required keys:
                "port" specifying the relay's port number,
                "host" specifying the host name (IP, DNS, etc.),
                "host-type" specifying the type of data in the "host" key.
        pool_metadata_url : str, optional
            URL to the pool's metadata JSON file.
        pool_metadata_hash : str, optional
            Optionally specify the hash of the metadata JSON file. If this is
            not specified and the pool_metadata_hash is, then the code will
            download the file from the URL and compute the hash.
        folder : str, Path, optional
            The directory where the generated files/certs will be placed.
        offline: bool, optional
            Flag to indicate if the transactions is being generated offline.
            If true (defaults to false), the transaction file is signed but
            not sent.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Get the hash of the JSON file if the URL is provided and the hash is
        # not specified.
        metadata_args = ""
        if pool_metadata_url is not None:
            if pool_metadata_hash is None:
                metadata_file = folder / "metadata_file_download.json"
                self._download_file(pool_metadata_url, metadata_file)
                result = self.run_cli(
                    f"{self.cli} stake-pool metadata-hash " f"--pool-metadata-file {metadata_file}"
                )
                pool_metadata_hash = result.stdout.strip()

            # Create the arg string for the pool cert.
            metadata_args = (
                f"--metadata-url {pool_metadata_url} " f"--metadata-hash {pool_metadata_hash}"
            )

        # Create the relay arg string. Basically, we need a port and host arg
        # but there can be different forms of the host argument. See the
        # caradno-cli documentation. The simpliest way I could figure was to
        # use a list of dictionaries where each dict represents a relay.
        relay_args = ""
        for relay in pool_relays:
            if "ipv4" in relay["host-type"]:
                host_arg = f"--pool-relay-ipv4 {relay['host']}"
            elif "ipv6" in relay["host-type"]:
                host_arg = f"--pool-relay-ipv4 {relay['host']}"
            elif "single" in relay["host-type"]:
                host_arg = f"--single-host-pool-relay {relay['host']}"
            elif "multi" in relay["host-type"]:
                relay_args += f"--multi-host-pool-relay {relay['host']}"
                continue  # No port info for this case
            else:
                continue  # Skip if invalid host type
            port_arg = f"--pool-relay-port {relay['port']}"
            relay_args += f"{host_arg} {port_arg} "

        # Create the argument string for the list of owner verification keys.
        owner_vkey_args = ""
        for key_path in owner_stake_vkeys:
            arg = f"--pool-owner-stake-verification-key-file {key_path} "
            owner_vkey_args += arg

        # Generate Stake pool registration certificate
        pool_cert_path = folder / (pool_name + "_registration.cert")
        self.run_cli(
            f"{self.cli} stake-pool registration-certificate "
            f"--cold-verification-key-file {pool_cold_vkey} "
            f"--vrf-verification-key-file {pool_vrf_key} "
            f"--pool-pledge {pool_pledge} "
            f"--pool-cost {pool_cost} "
            f"--pool-margin {pool_margin/100} "
            f"--pool-reward-account-verification-key-file {pool_reward_vkey} "
            f"{owner_vkey_args} {relay_args} {metadata_args} "
            f"{self.network} --out-file {pool_cert_path}"
        )

        # TODO: Edit the cert free text?

        # Generate delegation certificate (pledge from each owner)
        del_cert_args = ""
        signing_key_args = ""
        for key_path in owner_stake_vkeys:
            key_path = Path(key_path)
            cert_path = key_path.parent / (key_path.stem + "_delegation.cert")
            del_cert_args += f"--certificate-file {cert_path} "
            self.run_cli(
                f"{self.cli} stake-address delegation-certificate "
                f"--stake-verification-key-file {key_path} "
                f"--cold-verification-key-file {pool_cold_vkey} "
                f"--out-file {cert_path}"
            )

        # Generate a list of owner signing key args.
        for key_path in owner_stake_skeys:
            signing_key_args += f"--signing-key-file {key_path} "

        # Get the pool deposit from the network genesis parameters.
        pool_deposit = 0  # re-registration doesn't require deposit

        # Get a list of UTXOs and sort them in decending order by value.
        utxos = self.get_utxos(payment_addr)
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Ensure the parameters file exists
        self.get_protocol_parameters()

        # Iterate through the UTXOs until we have enough funds to cover the
        # transaction. Also, create the tx_in string for the transaction.
        tx_name = datetime.now().strftime("reg_pool_%Y-%m-%d_%Hh%Mm%Ss")
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        utxo_total = 0
        min_fee = 1  # make this start greater than utxo_total
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f" --tx-in {utxo['TxHash']}#{utxo['TxIx']}"

            # Build a transaction draft
            self.run_cli(
                f"{self.cli} transaction build-raw{tx_in_str} "
                f"--tx-out {payment_addr}+0 --ttl 0 --fee 0 "
                f"--out-file {tx_draft_file} "
                f"--certificate-file {pool_cert_path} {del_cert_args}"
            )

            # Calculate the minimum fee
            nwit = len(owner_stake_skeys) + 2
            min_fee = self.calc_min_fee(
                tx_draft_file, utxo_count, tx_out_count=1, witness_count=nwit
            )

            if utxo_total > (min_fee + pool_deposit):
                break

        if utxo_total < min_fee:
            cost_ada = (min_fee + pool_deposit) / 1_000_000
            utxo_total_ada = utxo_total / 1_000_000
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. Account "
                f"{payment_addr} cannot pay transaction costs of {cost_ada} "
                f"lovelaces because it only contains {utxo_total_ada} ADA."
            )

        # Build the transaction to submit the pool certificate and delegation
        # certificate(s) to the blockchain.
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        self.run_cli(
            f"{self.cli} transaction build-raw{tx_in_str} "
            f"--tx-out {payment_addr}+{utxo_total - min_fee - pool_deposit} "
            f"--ttl {ttl} --fee {min_fee} --out-file {tx_raw_file} "
            f"--certificate-file {pool_cert_path} {del_cert_args}"
        )

        # Sign the transaction with both the payment and stake keys.
        tx_signed_file = Path(self.working_dir) / (tx_name + ".signed")
        self.run_cli(
            f"{self.cli} transaction sign "
            f"--tx-body-file {tx_raw_file} --signing-key-file {payment_skey} "
            f"{signing_key_args} --signing-key-file {pool_cold_skey} "
            f"{self.network} --out-file {tx_signed_file}"
        )

        # Delete the transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)
            self._cleanup_file(tx_raw_file)

        # Submit the transaction
        if not offline:
            self.submit_transaction(tx_signed_file, cleanup)
        else:
            self.logger.info(f"Signed transaction file saved to: {tx_signed_file}")

    def retire_stake_pool(
        self,
        remaining_epochs,
        genesis_file,
        cold_vkey,
        cold_skey,
        payment_skey,
        payment_addr,
        cleanup=True,
    ):
        """Retire a stake pool using the stake pool keys.

        To retire the stake pool we need to:
        - Create a deregistration certificate and
        - Submit the certificate to the blockchain with a transaction.

        The deregistration certificate contains the epoch in which we want to
        retire the pool. This epoch must be after the current epoch and not
        later than eMax epochs in the future, where eMax is a protocol
        parameter.

        Parameters
        ----------
        remaining_epochs : int
            Epochs remaining before pool should be deregistered.
        genesis_file : str or Path
            Path to the genesis file.
        cold_vkey : str or Path
            Path to the pool's cold verification key.
        cold_skey : str or Path
            Path to the pool's cold signing key.
        payment_skey : str or Path
            Path to the payment signing key.
        payment_addr : str
            Address of the payment key.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # Get the network parameters
        self.get_protocol_parameters()
        e_max = self.get_protocol_parameters().get("eMax")

        # Make sure the remaining epochs is a valid number.
        if remaining_epochs < 1:
            remaining_epochs = 1
        elif remaining_epochs > e_max:
            raise NodeCLIError(
                f"Invalid number of remaining epochs ({remaining_epochs}) "
                f"prior to pool retirement. The maximum is {e_max}."
            )

        # Get the network genesis parameters
        with open(genesis_file, "r") as genfile:
            genesis_parameters = json.load(genfile)
        epoch_length = genesis_parameters["epochLength"]

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Get the current epoch
        epoch = tip // epoch_length

        # Create deregistration certificate
        pool_dereg = self.working_dir / "pool.dereg"
        self.run_cli(
            f"{self.cli} stake-pool deregistration-certificate "
            f"--cold-verification-key-file {cold_vkey} "
            f"--epoch {epoch + remaining_epochs} --out-file {pool_dereg}"
        )

        # Get a list of UTXOs and sort them in decending order by value.
        utxos = self.get_utxos(payment_addr)
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)

        # Iterate through the UTXOs until we have enough funds to cover the
        # transaction. Also, create the tx_in string for the transaction.
        tx_draft_file = self.working_dir / "pool_dereg_tx.draft"
        utxo_total = 0
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f" --tx-in {utxo['TxHash']}#{utxo['TxIx']}"

            # Build a transaction draft
            self.run_cli(
                f"{self.cli} transaction build-raw{tx_in_str} "
                f"--tx-out {payment_addr}+0 --ttl 0 --fee 0 "
                f"--out-file {tx_draft_file} --certificate-file {pool_dereg}"
            )

            # Calculate the minimum fee
            min_fee = self.calc_min_fee(tx_draft_file, utxo_count, tx_out_count=1, witness_count=2)

            if utxo_total > min_fee:
                break

        if utxo_total < min_fee:
            # cost_ada = min_fee/1_000_000
            utxo_total_ada = utxo_total / 1_000_000
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. Account "
                f"{payment_addr} cannot pay transaction costs of {min_fee} "
                f"lovelaces because it only contains {utxo_total_ada} ADA."
            )

        # Build the raw transaction
        tx_raw_file = self.working_dir / "pool_dereg_tx.raw"
        self.run_cli(
            f"{self.cli} transaction build-raw{tx_in_str} "
            f"--tx-out {payment_addr}+{utxo_total - min_fee} --ttl {ttl} "
            f"--fee {min_fee} --out-file {tx_raw_file} "
            f"--certificate-file {pool_dereg}"
        )

        # Sign it with both the payment signing key and the cold signing key.
        tx_signed_file = self.working_dir / "pool_dereg_tx.signed"
        self.run_cli(
            f"{self.cli} transaction sign "
            f"--tx-body-file {tx_raw_file} "
            f"--signing-key-file {payment_skey} "
            f"--signing-key-file {cold_skey} "
            f"{self.network} --out-file {tx_signed_file}"
        )

        # Submit the transaction
        self.run_cli(f"{self.cli} transaction submit " f"--tx-file {tx_signed_file} {self.network}")

        # Delete the transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)
            self._cleanup_file(tx_raw_file)
            self._cleanup_file(tx_signed_file)

    def get_stake_pool_id(self, cold_vkey) -> str:
        """Return the stake pool ID associated with the supplied cold key.

        Parameters
        ----------
        cold_vkey : str or Path
            Path to the pool's cold verification key.

        Returns
        ----------
        str
            The stake pool id.
        """
        result = self.run_cli(f"{self.cli} stake-pool id " f"--verification-key-file {cold_vkey}")
        pool_id = result.stdout
        return pool_id

    def get_leadership_schedule(
        self, genesis_file, pool_vrf_key, pool_id, current_epoch, next_epoch
    ) -> str:
        """Return the stake pool slot leadership schedule for the current
        or next epoch (Note: This command takes a few minutes to complete)

        Parameters
        ----------
        genesis_file : str or Path
            Path to the Shelley genesis file.
        pool_vrf_key : str or Path
            Path to the pool's verification key.
        pool_id : str
            The stake pool id.
        current_epoch : bool
            Flag to indicate whether to query slots for the current epoch.
        next_epoch : bool
            Flag to indicate whether to query slots for the next epoch.

        Returns
        ----------
        str
            The slot leadership schedule for the current and/or next epoch.

        --genesis ../relay1/mainnet-shelley-genesis.json --vrf-signing-key-file FAITH_vrf.skey --stake-pool-id 383696c7f29a9a49c1da49ed35bebbd6097cea5b58a95da5c7df27ee --next

        """
        # Must specify current or next epoch flag (but can't specify both)
        if current_epoch:
            flag = "--current "
        elif next_epoch:
            flag = "--next "
        if flag == "":
            raise NodeCLIError(f"Must set current_epoch or next_epoch argument to True.")

        result = self.run_cli(
            f"{self.cli} query leadership-schedule {self.network} "
            f"--genesis {genesis_file} "
            f"--vrf-signing-key-file {pool_vrf_key} "
            f"--stake-pool-id {pool_id} "
            f"{flag} "
        )
        schedule = result.stdout
        return schedule

    def claim_staking_rewards(
        self,
        stake_addr,
        stake_skey,
        receive_addr,
        payment_skey,
        payment_addr=None,
        offline=False,
        cleanup=True,
    ):
        """Withdraw staking address rewards to a spending address.

        Thanks to @ATADA_Stakepool who's scripts greatly influenced the
        development of this function. https://github.com/gitmachtl/scripts

        Parameters
        ----------
        stake_addr : str
            Staking address from which to withdraw the rewards.
        stake_skey : str or Path
            Path to the staking address signing key.
        receive_addr : str
            Spending address to receive the rewards.
        payment_skey : str or Path
            Path to the signing key for the account paying the tx fees.
        payment_addr : str, optional
            Optionally use a second account to pay the tx fees.
        offline: bool, optional
            Flag to indicate if the transactions is being generated offline.
            If true (defaults to false), the transaction file is signed but
            not sent.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # Calculate the amount to withdraw.
        rewards = self.get_rewards_balance(stake_addr)
        if rewards <= 0.0:
            raise NodeCLIError(f"No rewards availible in stake address {stake_addr}.")
        withdrawal_str = f"{stake_addr}+{rewards}"

        # Get a list of UTXOs and sort them in decending order by value.
        if payment_addr is None:
            payment_addr = receive_addr
        utxos = self.get_utxos(payment_addr)
        if len(utxos) < 1:
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. "
                f"Account {payment_addr} cannot pay transaction costs because "
                "it does not contain any ADA."
            )
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)

        # Build a transaction name
        tx_name = datetime.now().strftime("claim_rewards_%Y-%m-%d_%Hh%Mm%Ss")

        # Ensure the parameters file exists
        self.get_protocol_parameters()

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Iterate through the UTXOs until we have enough funds to cover the
        # transaction. Also, create the tx_in string for the transaction.
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        utxo_total = 0
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f" --tx-in {utxo['TxHash']}#{utxo['TxIx']}"

            # If the address receiving the funds is also paying the TX fee.
            if payment_addr == receive_addr:
                # Build a transaction draft
                self.run_cli(
                    f"{self.cli} transaction build-raw{tx_in_str} "
                    f"--tx-out {receive_addr}+0 --ttl 0 --fee 0 "
                    f"--withdrawal {withdrawal_str} --out-file {tx_draft_file}"
                )

                # Calculate the minimum fee
                min_fee = self.calc_min_fee(
                    tx_draft_file, utxo_count, tx_out_count=1, witness_count=2
                )

            # If another address is paying the TX fee.
            else:
                # Build a transaction draft
                self.run_cli(
                    f"{self.cli} transaction build-raw{tx_in_str} "
                    f"--tx-out {receive_addr}+0 --tx-out {payment_addr}+0 "
                    f"--ttl 0 --fee 0 --withdrawal {withdrawal_str} "
                    f"--out-file {tx_draft_file}"
                )

                # Calculate the minimum fee
                min_fee = self.calc_min_fee(
                    tx_draft_file, utxo_count, tx_out_count=2, witness_count=2
                )

            # If we have enough in the UTXO we are done, otherwise, continue.
            if utxo_total > min_fee:
                break

        if utxo_total < min_fee:
            cost_ada = min_fee / 1_000_000
            utxo_total_ada = utxo_total / 1_000_000
            a = receive_addr if payment_addr == receive_addr else payment_addr
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. "
                f"Account {a} cannot pay transaction costs of {cost_ada} "
                f"ADA because it only contains {utxo_total_ada} ADA."
            )

        # Build the transaction.
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        if payment_addr == receive_addr:
            # If the address receiving the funds is also paying the TX fee.
            self.run_cli(
                f"{self.cli} transaction build-raw{tx_in_str} "
                f"--tx-out {receive_addr}+{utxo_total - min_fee + rewards} "
                f"--ttl {ttl} --fee {min_fee} --withdrawal {withdrawal_str} "
                f"--out-file {tx_raw_file}"
            )
        else:
            # If another address is paying the TX fee.
            self.run_cli(
                f"{self.cli} transaction build-raw{tx_in_str} "
                f"--tx-out {payment_addr}+{utxo_total - min_fee} "
                f"--tx-out {receive_addr}+{rewards} "
                f"--ttl {ttl} --fee {min_fee} --withdrawal {withdrawal_str} "
                f"--out-file {tx_raw_file}"
            )

        # Sign the transaction with both the payment and stake keys.
        tx_signed_file = Path(self.working_dir) / (tx_name + ".signed")
        self.run_cli(
            f"{self.cli} transaction sign "
            f"--tx-body-file {tx_raw_file} --signing-key-file {payment_skey} "
            f"--signing-key-file {stake_skey} {self.network} "
            f"--out-file {tx_signed_file}"
        )

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)
            self._cleanup_file(tx_raw_file)

        # Submit the transaction
        if not offline:
            self.submit_transaction(tx_signed_file, cleanup)
        else:
            self.logger.info(f"Signed transaction file saved to: {tx_signed_file}")

    def convert_itn_keys(self, itn_prv_key, itn_pub_key, folder=None) -> str:
        """Convert ITN account keys to Shelley staking keys.

        Parameters
        ----------
        itn_prv_key : str or Path
            Path to the ITN private key file.
        itn_pub_key : str or Path
            Path to the ITN public key file.
        folder : str or Path, optional
            The directory where the generated files/certs will be placed.

        Returns
        -------
        str
            New Shelley staking wallet address.

        Raises
        ------
        NodeCLIError
            If the private key is not in a known format.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Open the private key file to check its format.
        prvkey = open(itn_prv_key, "r").read()

        # Convert the private key
        skey_file = folder / (Path(itn_prv_key).stem + "_shelley_staking.skey")
        if prvkey[:8] == "ed25519e":
            self.run_cli(
                f"{self.cli} key convert-itn-extended-key "
                f"--itn-signing-key-file {itn_prv_key} "
                f"--out-file {skey_file}"
            )
        elif prvkey[:8] == "ed25519b":
            self.run_cli(
                f"{self.cli} key convert-itn-bip32-key "
                f"--itn-signing-key-file {itn_prv_key} "
                f"--out-file {skey_file}"
            )
        elif prvkey[:7] == "ed25519":
            self.run_cli(
                f"{self.cli} key convert-itn-key "
                f"--itn-signing-key-file {itn_prv_key} "
                f"--out-file {skey_file}"
            )
        else:
            raise NodeCLIError("Invalid ITN private key format.")

        # Convert the public key
        vkey_file = folder / (Path(itn_pub_key).stem + "_shelley_staking.vkey")
        self.run_cli(
            f"{self.cli} key convert-itn-key "
            f"--itn-verification-key-file {itn_pub_key} "
            f"--out-file {vkey_file}"
        )

        # Create the staking address
        addr_file = folder / (Path(itn_pub_key).stem + "_shelley_staking.addr")
        self.run_cli(
            f"{self.cli} stake-address build "
            f"--stake-verification-key-file {vkey_file} "
            f"--out-file {addr_file} {self.network}"
        )

        # Read the file and return the staking address.
        addr = self._load_text_file(addr_file).strip()
        return addr

    def get_rewards_balance(self, stake_addr) -> int:
        """Return the balance in a Shelley staking rewards account.

        Parameters
        ----------
        addr : str
            Staking address.

        Returns
        ----------
        int
            Rewards balance in lovelaces.
        """
        result = self.run_cli(
            f"{self.cli} query stake-address-info --address " f"{stake_addr} {self.network}"
        )
        if "Failed" in result.stdout:
            raise NodeCLIError(result.stdout)
        if len(result.stderr) > 0:
            raise NodeCLIError(result.stderr)
        info = json.loads(result.stdout)
        balance = sum(b["rewardAccountBalance"] for b in info)
        return balance

    def empty_account(self, to_addr, from_addr, key_file, offline=False, cleanup=True):
        """Send all ADA contained in one address to another address.

        Parameters
        ----------
        to_addr : str
            Address to send the ADA to.
        from_addr : str
            Address to send the ADA from.
        key_file : str or Path
            Path to the send address signing key file.
        offline: bool, optional
            Flag to indicate if the transactions is being generated offline.
            If true (defaults to false), the transaction file is signed but
            not sent.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # Get the address balance
        bal = self.query_balance(from_addr)

        # Build a transaction name
        tx_name = datetime.now().strftime("empty_acct_%Y-%m-%d_%Hh%Mm%Ss")

        # Get a list of UTxOs and create the tx_in string.
        tx_in_str = ""
        utxos = self.get_utxos(from_addr)
        for utxo in utxos:
            tx_in_str += f" --tx-in {utxo['TxHash']}#{utxo['TxIx']}"

        # Build a transaction draft
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        self.run_cli(
            f"{self.cli} transaction build-raw{tx_in_str} "
            f"--tx-out {to_addr}+0 "
            f"--ttl 0 --fee 0 --out-file {tx_draft_file}"
        )

        # Calculate the minimum fee
        min_fee = self.calc_min_fee(tx_draft_file, len(utxos), tx_out_count=2, witness_count=1)

        if min_fee > bal:
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. "
                f"Account {from_addr} cannot send {bal/1_000_000} ADA plus "
                f"fees of {min_fee/1_000_000} ADA to account {to_addr} "
                f"because it only contains {bal/1_000_000.} ADA."
            )
            # Maybe this should fail more gracefully, but higher level logic
            # can also just catch the error and handle it.

        # Determine the slot where the transaction will become invalid. Get the
        # current slot number and add a buffer to it.
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Build the transaction
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        self.run_cli(
            f"{self.cli} transaction build-raw{tx_in_str} "
            f"--tx-out {to_addr}+{(bal - min_fee):.0f} "
            f"--ttl {ttl} --fee {min_fee} --out-file {tx_raw_file}"
        )

        # Sign the transaction with the signing key
        tx_signed_file = self.sign_transaction(tx_raw_file, [key_file])

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_raw_file)

        # Submit the transaction
        if not offline:
            self.submit_transaction(tx_signed_file, cleanup)
        else:
            self.logger.info(f"Signed transaction file saved to: {tx_signed_file}")

    def days2slots(self, days, genesis_file) -> int:
        """Convert time in days to time in slots.

        Parameters
        ----------
        days : float
            The number of days to convert to the number of slots.
        genesis_file : str or Path
            Path to the Shelley genesis file.

        Returns
        -------
        int
            Nearest integer value of slots occuring in the specified duration.
        """

        # Convert days to seconds.
        dur_secs = days * 24 * 60 * 60

        # Get the info from the network genesis parameters.
        json_data = self._load_text_file(genesis_file)
        slot_dur_secs = json.loads(json_data)["slotLength"]

        return int(dur_secs / slot_dur_secs)

    def days2epochs(self, days, genesis_file) -> float:
        """Convert time in days to time in epochs.

        Parameters
        ----------
        days : float
            The number of days to convert to the number of epochs.
        genesis_file : str or Path
            Path to the Shelley genesis file.

        Returns
        -------
        float
            Number of epochs occuring in the specified duration.
        """

        # Convert the days to the number of slots
        dur_slots = self.days2slots(days, genesis_file)

        # Get the info from the network genesis parameters.
        json_data = self._load_text_file(genesis_file)
        epoch_dur_slots = json.loads(json_data)["epochLength"]

        return float(dur_slots) / epoch_dur_slots

    def generate_policy(self, script_path) -> str:
        """Generate a minting policy ID.

        Parameters
        ----------
        script_path : str or Path
            Path to the minting policy definition script.

        Returns
        -------
        str
            The minting policy id (script hash).
        """

        # Submit the transaction
        result = self.run_cli(f"{self.cli} transaction policyid " f" --script-file {script_path}")
        return result.stdout

    def _get_token_utxos(self, addr, policy_id, asset_names, quantities):
        """Get a list of UTxOs that contain the desired assets.

        Parameters
        ----------
        addr : str
            The address containing the UTxOs with the desired assets.
        policy_id : str
            Policy ID for the assets (single policy only).
        asset_names : list
            List of asset names (hex format strings).
        quantities : list
            List of quantities (integers) of the tokens.
        """

        # Make a list of all asset names (unique!)
        send_assets = {}
        for name, amt in zip(asset_names, quantities):
            asset = f"{policy_id}.{name}" if name else policy_id
            if asset in send_assets:
                send_assets[asset] += amt
            else:
                send_assets[asset] = amt

        # Get a list of UTxOs for the transaction
        utxos = []
        input_str = ""
        input_lovelace = 0
        for i, asset in enumerate(send_assets.keys()):

            # Find all the UTxOs containing the assets desired. This may take a
            # while if there are a lot of tokens!
            utxos_found = self.get_utxos(addr, filter=asset)

            # Iterate through the UTxOs and only take enough needed to process
            # the requested amount of tokens. Also, only create a list of unique
            # UTxOs.
            asset_count = 0
            for utxo in utxos_found:

                # UTxOs could show up twice if they contain multiple different
                # assets. Only put them in the list once.
                if utxo not in utxos:
                    utxos.append(utxo)

                    # If this is a unique UTxO being added to the list, keep
                    # track of the total Lovelaces and add it to the
                    # transaction input string.
                    input_lovelace += int(utxo["Lovelace"])
                    input_str += f"--tx-in {utxo['TxHash']}#{utxo['TxIx']} "

                asset_count += int(utxo[asset])
                if asset_count >= quantities[i]:
                    break

            if asset_count < quantities[i]:
                raise NodeCLIError(f"Not enought {asset} tokens availible.")

        # If we get to this point, we have enough UTxOs to cover the requested
        # tokens. Next we need to build lists of the output and return tokens.
        output_tokens = {}
        return_tokens = {}
        for utxo in utxos:
            # Iterate through the UTxO entries.
            for k in utxo.keys():
                if k in ["TxHash", "TxIx", "Lovelace"]:
                    pass  # These are the UTxO IDs in every UTxO.
                elif k in send_assets:
                    # These are the native assets requested.
                    if k in output_tokens:
                        output_tokens[k] += int(utxo[k])
                    else:
                        output_tokens[k] = int(utxo[k])

                    # If the UTxOs selected for the transaction contain more
                    # tokens than requested, clip the number of output tokens
                    # and put the remainder as returning tokens.
                    if output_tokens[k] > send_assets[k]:
                        return_tokens[k] = output_tokens[k] - send_assets[k]
                        output_tokens[k] = send_assets[k]
                else:
                    # These are tokens that are not being requested so they just
                    # need to go back to the wallet in another output.
                    if k in return_tokens:
                        return_tokens[k] += int(utxo[k])
                    else:
                        return_tokens[k] = int(utxo[k])

        # Note: at this point output_tokens should be the same as send_assets.
        # It was necessary to build another dict of output tokens as we
        # iterated through the list of UTxOs for proper accounting.

        # Return the computed results as a tuple to be used for building a token
        # transaction.
        return (input_str, input_lovelace, output_tokens, return_tokens)

    def build_send_tx(
        self,
        to_addr,
        from_addr,
        quantity,
        policy_id,
        asset_name=None,
        ada=0.0,
        folder=None,
        cleanup=True,
    ):
        """Build a transaction for sending an integer number of native assets
        from one address to another.

        Opinionated: Only send 1 type of Native Token at a time. Will only
        combine additional ADA-only UTxOs when paying for the transactions fees
        and minimum UTxO ADA values if needed.

        Parameters
        ----------
        to_addr : str
            Address to send the asset to.
        from_addr : str
            Address to send the asset from.
        quantity : float
            Integer number of assets to send.
        policy_id : str
            Policy ID of the asset to be sent.
        asset_name : str, optional
            Asset name if applicable (ASCII strings).
        ada : float, optional
            Optionally set the amount of ADA to be sent with the token.
        folder : str or Path, optional
            The working directory for the function.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).
        """

        # This is a constant modifier to determine the minimum ADA for breaking
        # off additional ADA into a separate UTxO. It essentially prevents
        # oscillations at potential bifurcation points where adding or taking
        # away a transaction output puts the extra ADA under or over the
        # minimum UTxO due to transaction fees. This parameter may need to be
        # tuned, but should be fairly small.
        minMult = 1.1

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Make sure the qunatity is positive.
        quantity = abs(quantity)

        # Convert asset name to hex
        asset_name = "".join("{:02x}".format(c) for c in asset_name.encode("utf-8"))

        # Get the required UTxO(s) for the requested token.
        (
            input_str,
            input_lovelace,
            output_tokens,
            return_tokens,
        ) = self._get_token_utxos(from_addr, policy_id, [asset_name], [quantity])

        # Build token input and output strings
        output_token_utxo_str = ""
        for token in output_tokens.keys():
            output_token_utxo_str += f" + {output_tokens[token]} {token}"
        return_token_utxo_str = ""
        for token in return_tokens.keys():
            return_token_utxo_str += f" + {return_tokens[token]} {token}"

        # Calculate the minimum ADA for the token UTxOs.
        min_utxo_out = utils.minimum_utxo(
            self.get_protocol_parameters(),
            output_tokens.keys()
        )
        min_utxo_ret = utils.minimum_utxo(
            self.get_protocol_parameters(),
            return_tokens.keys()
        )

        # Lovelace to send with the Token
        utxo_out = max([min_utxo_out, int(ada * 1_000_000)])

        # Lovelaces to return to the wallet
        utxo_ret = min_utxo_ret
        if len(return_tokens) == 0:
            utxo_ret = 0

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Ensure the parameters file exists
        min_utxo = self.get_min_utxo()

        # Create a metadata string
        meta_str = ""  # Maybe add later

        # Get a list of Lovelace only UTxOs and sort them in ascending order
        # by value. We may not end up needing these.
        ada_utxos = self.get_utxos(from_addr, filter="Lovelace")
        ada_utxos.sort(key=lambda k: k["Lovelace"], reverse=False)

        # Create a name for the transaction files.
        tx_name = datetime.now().strftime("tx_%Y-%m-%d_%Hh%Mm%Ss")
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")

        # Create a TX out string given the possible scenarios.
        use_ada_utxo = False
        if len(return_tokens) == 0:
            if (input_lovelace - utxo_out) < minMult * min_utxo:
                output_str = f'--tx-out "{to_addr}+0{output_token_utxo_str}" '
            else:
                output_str = (
                    f'--tx-out "{to_addr}+0{output_token_utxo_str}" ' f'--tx-out "{from_addr}+0" '
                )
                use_ada_utxo = True
        else:
            if (input_lovelace - utxo_out - utxo_ret) < minMult * min_utxo:
                output_str = (
                    f'--tx-out "{to_addr}+0{output_token_utxo_str}" '
                    f'--tx-out "{from_addr}+0{return_token_utxo_str}" '
                )
            else:
                output_str = (
                    f'--tx-out "{to_addr}+0{output_token_utxo_str}" '
                    f'--tx-out "{from_addr}+0{return_token_utxo_str}" '
                    f'--tx-out "{from_addr}+0" '
                )
                use_ada_utxo = True

        # Calculate the minimum transaction fee as it is right now with only the
        # minimum UTxOs needed for the tokens.
        self.run_cli(
            f"{self.cli} transaction build-raw {input_str}"
            f"{output_str} --ttl 0 --fee 0 {meta_str} "
            f"{self.era} --out-file {tx_draft_file}"
        )
        min_fee = self.calc_min_fee(
            tx_draft_file,
            input_str.count("--tx-in "),
            tx_out_count=output_str.count("--tx-out "),
            witness_count=1,
        )

        # If we don't have enough ADA, we will have to add another UTxO to cover
        # the transaction fees.
        if input_lovelace < (min_fee + utxo_ret + utxo_out):

            # Iterate through the UTxOs until we have enough funds to cover the
            # transaction. Also, update the tx_in string for the transaction.
            for idx, utxo in enumerate(ada_utxos):
                input_lovelace += int(utxo["Lovelace"])
                input_str += f"--tx-in {utxo['TxHash']}#{utxo['TxIx']} "

                self.run_cli(
                    f"{self.cli} transaction build-raw {input_str}"
                    f"{output_str} --ttl 0 --fee 0 {meta_str} "
                    f"{self.era} --out-file {tx_draft_file}"
                )
                min_fee = self.calc_min_fee(
                    tx_draft_file,
                    input_str.count("--tx-in "),
                    tx_out_count=output_str.count("--tx-out "),
                    witness_count=1,
                )

                # If we don't have enough ADA here, then go ahead and add another
                # ADA only UTxO.
                if input_lovelace < (min_fee + utxo_ret + utxo_out):
                    continue

                # If we do have enough to cover the needed output and fees, check
                # if we need to add a second UTxO with the extra ADA.
                if (
                    input_lovelace - (min_fee + utxo_ret + utxo_out) > minMult * min_utxo
                    and output_str.count("--tx-out ") < 3
                ):

                    self.run_cli(
                        f"{self.cli} transaction build-raw {input_str}"
                        f"{output_str} --tx-out {from_addr}+0 "
                        f"--ttl 0 --fee 0 {meta_str} "
                        f"{self.era} --out-file {tx_draft_file}"
                    )
                    min_fee = self.calc_min_fee(
                        tx_draft_file,
                        input_str.count("--tx-in "),
                        tx_out_count=output_str.count("--tx-out "),
                        witness_count=1,
                    )

                    # Flag that we are using an extra ADA only UTxO.
                    use_ada_utxo = True

                # We should be good here
                break  # <-- Important!

        # Handle the error case where there is not enough inputs for the output
        if input_lovelace < (min_fee + utxo_ret + utxo_out):
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. Account "
                f"{from_addr} needs an additional ADA only UTxO."
            )

        # Figure out the amount of ADA to put with the different UTxOs.
        # If we have tokens being returned to the wallet, only keep the minimum
        # ADA in that UTxO and make an extra ADA only UTxO.
        utxo_ret_ada = 0
        if use_ada_utxo:
            if len(return_tokens) == 0:
                utxo_ret_ada = input_lovelace - utxo_out - min_fee
            else:
                utxo_ret_ada = input_lovelace - utxo_ret - utxo_out - min_fee
        else:
            if len(return_tokens) == 0:
                min_fee += input_lovelace - utxo_out - min_fee
            else:
                utxo_ret += input_lovelace - utxo_ret - utxo_out - min_fee

        # Build the transaction to send to the blockchain.
        token_return_utxo_str = ""
        if utxo_ret > 0:
            token_return_utxo_str = f'--tx-out "{from_addr}+{utxo_ret}{return_token_utxo_str}"'
        token_return_ada_str = ""
        if utxo_ret_ada > 0:
            token_return_ada_str = f"--tx-out {from_addr}+{utxo_ret_ada}"
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")

        self.run_cli(
            f"{self.cli} transaction build-raw {input_str}"
            f'--tx-out "{to_addr}+{utxo_out}{output_token_utxo_str}" '
            f"{token_return_utxo_str} {token_return_ada_str} "
            f"--ttl {ttl} --fee {min_fee} {self.era} "
            f"--out-file {tx_raw_file}"
        )

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)

        # Return the path to the raw transaction file.
        return tx_raw_file

    def build_mint_transaction(
        self,
        policy_id,
        asset_names,
        quantities,
        payment_addr,
        witness_count,
        minting_script,
        tx_metadata=None,
        ada=0.0,
        folder=None,
        cleanup=True,
    ) -> str:
        """Build the transaction for minting a new native asset.

        Requires a running and synced node.

        Parameters
        ----------
        policy_id : str
            The minting policy ID (generated from the signature script).
        asset_names : list
            A list of asset names (ASCII strings).
        quantities : list
            A list of asset quantities.
        payment_addr : str
            The address paying the minting fees. Will also own the tokens.
        witness_count : int
            The number of signing keys.
        minting_script:

        tx_metadata : str or Path, optional
            Path to the metadata stored in a JSON file.
        ada : float, optional
            Optionally set the amount of ADA to be included with the tokens.
        folder : str or Path, optional
            The working directory for the function. Will use the Shelley
            object's working directory if node is given.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).

        Return
        ------
        str
            Path to the mint transaction file generated.
        """

        # This is a constant modifier to determine the minimum ADA for breaking
        # off additional ADA into a separate UTxO. It essentially prevents
        # oscillations at potential bifurcation points where adding or taking
        # away a transaction output puts the extra ADA under or over the
        # minimum UTxO due to transaction fees. This parameter may need to be
        # tuned bust should be fairly small.
        minMult = 2.1

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Convert asset names to hex strings
        for n, name in enumerate(asset_names):
            asset_names[n] = "".join("{:02x}".format(c) for c in name.encode("utf-8"))

        # Make sure all names are unique and the quantities match the names.
        # Giving a name is optional. So, if no names, one quantitiy value is
        # required.
        asset_names = list(set(asset_names))
        if len(asset_names) == 0:
            if len(quantities) != 1:
                raise NodeCLIError("Invalid list of quantities.")
        else:
            if len(quantities) != len(asset_names):
                raise NodeCLIError("Invalid combination of names and quantities.")
        for q in quantities:
            if q < 1:
                raise NodeCLIError("Invalid quantity for minting!")

        # Get a list of ADA only UTXOs and sort them in ascending order by
        # value.
        utxos = self.get_utxos(payment_addr, filter="Lovelace")
        utxos.sort(key=lambda k: k["Lovelace"], reverse=True)
        if len(utxos) < 1:
            raise NodeCLIError("No ADA only UTxOs for minting.")

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Calculate the minimum UTxO
        min_utxo = self.get_min_utxo()
        mint_assets = [f"{policy_id}.{name}" for name in asset_names]
        if len(mint_assets) == 0:
            mint_assets = [policy_id]
        min_love = utils.minimum_utxo(self.get_protocol_parameters(), mint_assets)

        # Lovelace to send with the Token
        utxo_out = max([min_love, int(ada * 1_000_000)])

        # Create minting string
        mint_str = ""
        if len(asset_names) == 0:
            mint_str += f"{quantities[0]} {policy_id}"
        else:
            for i, name in enumerate(asset_names):
                sep = " + " if i != 0 else ""
                mint_str += f"{sep}{quantities[i]} {policy_id}.{name}"

        # Create a metadata string
        meta_str = ""
        if tx_metadata is not None:
            meta_str = f"--metadata-json-file {tx_metadata}"

        # Create a minting script string
        script_str = f"--minting-script-file {minting_script}"

        tx_name = datetime.now().strftime("tx_%Y-%m-%d_%Hh%Mm%Ss")
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")

        # Iterate through the ADA only UTxOs until we have enough funds to
        # cover the transaction. Also, create the tx_in string for the
        # transaction.
        utxo_ret_ada = 0
        utxo_total = 0
        tx_in_str = ""
        for idx, utxo in enumerate(utxos):
            # Add an availible UTxO to the list and then check to see if we now
            # have enough lovelaces to cover the transaction fees and what we
            # want with the tokens.
            utxo_count = idx + 1
            utxo_total += int(utxo["Lovelace"])
            tx_in_str += f"--tx-in {utxo['TxHash']}#{utxo['TxIx']} "

            # Build a transaction draft with a single output.
            self.run_cli(
                f"{self.cli} transaction build-raw {tx_in_str}"
                f'--tx-out "{payment_addr}+{utxo_total}+{mint_str}" '
                f"--ttl 0 --fee 0 "
                f'--mint "{mint_str}" {script_str} {meta_str} '
                f"{self.era} --out-file {tx_draft_file}"
            )

            # Calculate the minimum fee for the transaction with a single
            # minting output.
            min_fee = self.calc_min_fee(
                tx_draft_file,
                utxo_count,
                tx_out_count=1,
                witness_count=witness_count,
            )

            # If we don't have enough ADA here, then go ahead and add another
            # ADA only UTxO.
            if utxo_total < (min_fee + utxo_out):
                continue

            # If we do have enough to cover the needed output and fees, check
            # if we need to add a second UTxO with the extra ADA.
            if utxo_total - (min_fee + utxo_out) > minMult * min_utxo:

                # Create a draft transaction with an extra ADA only UTxO.
                self.run_cli(
                    f"{self.cli} transaction build-raw {tx_in_str}"
                    f'--tx-out "{payment_addr}+{utxo_total}+{mint_str}" '
                    f'--tx-out "{payment_addr}+0" --ttl 0 --fee 0 '
                    f'--mint "{mint_str}" {script_str} {meta_str} '
                    f"{self.era} --out-file {tx_draft_file}"
                )

                # Calculate the minimum fee for the transaction with an extra
                # ADA only UTxO.
                min_fee = self.calc_min_fee(
                    tx_draft_file,
                    utxo_count,
                    tx_out_count=2,
                    witness_count=witness_count,
                )

                # Save the amount of ADA that we are returning in a separate
                # UTxO.
                utxo_ret_ada = utxo_total - (min_fee + utxo_out)

            else:
                # If we are staying with the single UTxO result. Make sure any
                # overages are just added to the output so the transaction
                # balances.
                utxo_out += utxo_total - (min_fee + utxo_out)

            # We should be good to go here.
            break

        # Handle the error case where there is not enough inputs for the output
        if utxo_total < (min_fee + utxo_out):
            cost_ada = (min_fee + utxo_out) / 1_000_000
            utxo_total_ada = utxo_total / 1_000_000
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. Account "
                f"{payment_addr} cannot pay tranction costs of {cost_ada} "
                f"ADA because it only contains {utxo_total_ada} ADA."
            )

        # Build the transaction to send to the blockchain.
        token_return_ada_str = ""
        if utxo_ret_ada > 0:
            token_return_ada_str = f"--tx-out {payment_addr}+{utxo_ret_ada}"
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        self.run_cli(
            f"{self.cli} transaction build-raw {tx_in_str}"
            f'--tx-out "{payment_addr}+{utxo_out}+{mint_str}" '
            f"{token_return_ada_str} --ttl {ttl} --fee {min_fee} "
            f'--mint "{mint_str}" {script_str} {meta_str} '
            f"{self.era} --out-file {tx_raw_file}"
        )

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)

        # Return the path to the raw transaction file.
        return tx_raw_file

    def build_burn_transaction(
        self,
        policy_id,
        asset_names,
        quantities,
        payment_addr,
        witness_count,
        minting_script,
        tx_metadata=None,
        folder=None,
        cleanup=True,
    ) -> str:
        """Build the transaction for burning a native asset.

        Requires a running and synced node.

        Parameters
        ----------
        policy_id : str
            The minting policy ID generated from the signature script--the
            same for all assets.
        asset_names : list
            List of asset names (same size as quantity list) [ASCII strings].
        quantities : list
            List of the numbers of each asset to burn.
        payment_addr : str
            The address paying the minting fees. Will also contain the tokens.
        witness_count : int
            The number of signing keys.
        tx_metadata : str or Path, optional
            Path to the metadata stored in a JSON file.
        folder : str or Path, optional
            The working directory for the function. Will use the Shelley
            object's working directory if node is given.
        cleanup : bool, optional
            Flag that indicates if the temporary transaction files should be
            removed when finished (defaults to True).

        Return
        ------
        str
            Path to the mint transaction file generated.
        """

        # Get a working directory to store the generated files and make sure
        # the directory exists.
        if folder is None:
            folder = self.working_dir
        else:
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)

        # Convert asset names to hex strings
        for n, name in enumerate(asset_names):
            asset_names[n] = "".join("{:02x}".format(c) for c in name.encode("utf-8"))

        # Make sure all names are unique and the quantities match the names.
        # Giving a name is optional. So, if no names, one quantitiy value is
        # required.
        asset_names = list(set(asset_names))
        if len(asset_names) == 0:
            if len(quantities) != 1:
                raise NodeCLIError("Invalid list of quantities.")
        else:
            if len(quantities) != len(asset_names):
                raise NodeCLIError("Invalid combination of names and quantities.")

        # Users may send the quantities in as negative values since we are
        # burining. However, the quantities must be positive for the
        # calculations prior to the transaction. The negative sign will be
        # added to the mint inputs appropriately.
        quantities = [abs(q) for q in quantities]

        # Get the required UTxO(s) for the requested token.
        (
            input_str,
            input_lovelace,
            output_tokens,
            return_tokens,
        ) = self._get_token_utxos(payment_addr, policy_id, asset_names, quantities)

        # Determine the TTL
        tip = self.get_tip()
        ttl = tip + self.ttl_buffer

        # Get the minimum ADA only UTxO size.
        min_utxo = self.get_min_utxo()

        # Create transaction strings for the tokens. The minting input string
        # and the UTxO string for any remaining tokens.
        burn_str = ""
        token_utxo_str = ""
        for i, asset in enumerate(output_tokens.keys()):
            sep = " + " if i != 0 else ""
            burn_str += f"{sep}{-1*output_tokens[asset]} {asset}"
        for asset in return_tokens.keys():
            token_utxo_str += f" + {return_tokens[asset]} {asset}"

        # Create a metadata string
        meta_str = ""
        if tx_metadata is not None:
            meta_str = f"--metadata-json-file {tx_metadata}"

        # Create a minting script string
        script_str = f"--minting-script-file {minting_script}"

        # Calculate the minimum fee and UTxO sizes for the transaction as it is
        # right now with only the minimum UTxOs needed for the tokens.
        tx_name = datetime.now().strftime("tx_%Y-%m-%d_%Hh%Mm%Ss")
        tx_draft_file = Path(self.working_dir) / (tx_name + ".draft")
        self.run_cli(
            f"{self.cli} transaction build-raw {input_str}"
            f'--tx-out "{payment_addr}+{input_lovelace}{token_utxo_str}" '
            f'--ttl 0 --fee 0 --mint "{burn_str}" {script_str} {meta_str} '
            f"{self.era} --out-file {tx_draft_file}"
        )
        min_fee = self.calc_min_fee(
            tx_draft_file,
            utxo_count := input_str.count("--tx-in "),
            tx_out_count=1,
            witness_count=witness_count,
        )
        min_utxo_ret = utils.minimum_utxo(self.get_protocol_parameters(), return_tokens.keys())

        # If we don't have enough ADA, we will have to add another UTxO to cover
        # the transaction fees.
        if input_lovelace < min_fee + min_utxo_ret:

            # Get a list of Lovelace only UTxOs and sort them in ascending order
            # by value.
            ada_utxos = self.get_utxos(payment_addr, filter="Lovelace")
            ada_utxos.sort(key=lambda k: k["Lovelace"], reverse=False)

            # Iterate through the UTxOs until we have enough funds to cover the
            # transaction. Also, update the tx_in string for the transaction.
            for utxo in ada_utxos:
                utxo_count += 1
                input_lovelace += int(utxo["Lovelace"])
                input_str += f"--tx-in {utxo['TxHash']}#{utxo['TxIx']} "

                # Build a transaction draft
                self.run_cli(
                    f"{self.cli} transaction build-raw {input_str}"
                    f'--tx-out "{payment_addr}+{input_lovelace}{token_utxo_str}" '
                    f'--ttl 0 --fee 0 --mint "{burn_str}" {script_str} {meta_str} '
                    f"{self.era} --out-file {tx_draft_file}"
                )

                # Calculate the minimum fee
                min_fee = self.calc_min_fee(
                    tx_draft_file,
                    utxo_count,
                    tx_out_count=1,
                    witness_count=witness_count,
                )

                # If we have enough Lovelaces to cover the transaction, we can stop
                # iterating through the UTxOs.
                if input_lovelace > (min_fee + min_utxo_ret):
                    break

        # Handle the error case where there is not enough inputs for the output
        if input_lovelace < min_fee + min_utxo_ret:
            raise NodeCLIError(
                f"Transaction failed due to insufficient funds. Account "
                f"{payment_addr} needs an additional ADA only UTxO."
            )

        # Build the transaction to the blockchain.
        utxo_amt = input_lovelace - min_fee
        if utxo_amt < min_utxo:
            min_fee = utxo_amt
            utxo_amt = 0
        tx_raw_file = Path(self.working_dir) / (tx_name + ".raw")
        self.run_cli(
            f"{self.cli} transaction build-raw {input_str}"
            f'--tx-out "{payment_addr}+{utxo_amt}{token_utxo_str}" '
            f'--ttl {ttl} --fee {min_fee} --mint "{burn_str}" {script_str} {meta_str} '
            f"{self.era} --out-file {tx_raw_file}"
        )

        # Delete the intermediate transaction files if specified.
        if cleanup:
            self._cleanup_file(tx_draft_file)

        # Return the path to the raw transaction file.
        return tx_raw_file


if __name__ == "__main__":
    # Not used as a script
    pass
