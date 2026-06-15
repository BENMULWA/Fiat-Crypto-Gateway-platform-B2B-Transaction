from __future__ import annotations
import json

from websockets.sync.client import connect, ClientConnection

from .logger import logger
from .model.ogmios_model import Jsonrpc
from .errors import InvalidResponseError

from .chainsync import FindIntersection, NextBlock
from .statequery import (
    AcquireLedgerState,
    QueryBlockHeight,
    QueryNetworkTip,
    QueryStartTime,
    QueryLedgerTip,
    QueryGenesisConfiguration,
    QueryEpoch,
    QueryEraStart,
    QueryEraSummaries,
    QueryLiveStakeDistribution,
    QueryStakePools,
    QueryUtxo,
    QueryProjectedRewards,
    QueryRewardsProvenance,
    QueryRewardAccountSummaries,
    QueryProtocolParameters,
    QueryProposedProtocolParameters,
    QueryConstitution,
    QueryConstitutionalCommittee,
    QueryTreasuryAndReserves,
)
from .mempool import AcquireMempool, ReleaseMempool, SizeOfMempool, HasTransaction, NextTransaction
from .txsubmit import SubmitTransaction, EvaluateTransaction


class Client:
    """
    Ogmios connection client

    A subset of Ogmios functions require the use of WebSockets. Therefore a
    WebSocket connection is preferred over HTTP. If http_only is set to True,
    functions that require WebSockets will not be available.

    If secure is set to False, ws / http will be used rather than wss / https

    :param host: The host of the Ogmios server
    :type host: str
    :param port: The port of the Ogmios server
    :type port: int
    :param secure: Use secure connection
    :type secure: bool
    :param http_only: Use HTTP connection
    :type http_only: bool
    :param compact: Use compact connection
    :type compact: bool
    :param rpc_version: The JSON-RPC version to use
    :type rpc_version: Jsonrpc
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1337,
        path: str = "",
        secure: bool = False,
        http_only: bool = False,
        rpc_version: Jsonrpc = Jsonrpc.field_2_0,
        additional_headers: dict = {},
    ) -> None:
        if http_only:
            protocol: str = "https" if secure else "http"
            # TODO: Implement HTTP connections
            logger.error("HTTP connections not implemented")
            raise NotImplementedError
        else:
            protocol: str = "wss" if secure else "ws"

        self.rpc_version = rpc_version
        connect_str: str = f"{protocol}://{host}:{port}/{path}"
        self.connection: ClientConnection = connect(
            connect_str, additional_headers=additional_headers
        )

        # Ogmios chainsync methods
        self.find_intersection = FindIntersection(self)
        self.next_block = NextBlock(self)

        # Ogmios statequery methods
        self.acquire_ledger_state = AcquireLedgerState(self)
        self.query_block_height = QueryBlockHeight(self)
        self.query_network_tip = QueryNetworkTip(self)
        self.query_ledger_tip = QueryLedgerTip(self)
        self.query_start_time = QueryStartTime(self)
        self.query_genesis_configuration = QueryGenesisConfiguration(self)
        self.query_epoch = QueryEpoch(self)
        self.query_era_start = QueryEraStart(self)
        self.query_era_summaries = QueryEraSummaries(self)
        self.query_live_stake_distribution = QueryLiveStakeDistribution(self)
        self.query_stake_pools = QueryStakePools(self)
        self.query_utxo = QueryUtxo(self)
        self.query_projected_rewards = QueryProjectedRewards(self)
        self.query_rewards_provenance = QueryRewardsProvenance(self)
        self.query_reward_account_summaries = QueryRewardAccountSummaries(self)
        self.query_protocol_parameters = QueryProtocolParameters(self)
        self.query_proposed_protocol_parameters = QueryProposedProtocolParameters(self)
        self.query_constitution = QueryConstitution(self)
        self.query_constitutional_committee = QueryConstitutionalCommittee(self)
        self.query_treasury_and_reserves = QueryTreasuryAndReserves(self)

        # Ogmios mempool methods
        self.acquire_mempool = AcquireMempool(self)
        self.release_mempool = ReleaseMempool(self)
        self.size_of_mempool = SizeOfMempool(self)
        self.has_transaction = HasTransaction(self)
        self.next_transaction = NextTransaction(self)

        # Ogmios txsubmit methods
        self.submit_transaction = SubmitTransaction(self)
        self.evaluate_transaction = EvaluateTransaction(self)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        """Close client connection when finished"""
        self.connection.close()

    def send(self, request: str) -> None:
        """Send a request to the Ogmios server

        :param request: The request to send
        :type request: str
        """
        self.connection.send(request)

    def receive(self) -> dict:
        """Receive a response from the Ogmios server

        :return: Request response
        """
        resp = json.loads(self.connection.recv())
        if resp.get("version"):
            raise InvalidResponseError(
                f"Invalid Ogmios version. ogmios-python only supports Ogmios server version v6.0.0 and above."
            )
        return resp
