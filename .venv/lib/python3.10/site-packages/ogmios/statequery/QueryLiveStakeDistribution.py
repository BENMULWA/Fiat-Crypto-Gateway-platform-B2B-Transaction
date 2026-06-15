from __future__ import annotations

from typing import Any, Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ogmios.client import Client

from ogmios.errors import InvalidMethodError, InvalidResponseError, ResponseError
from ogmios.logger import logger
import ogmios.model.ogmios_model as om
import ogmios.model.model_map as mm

# pyright can't properly parse models, so we need to ignore its type checking
#  (pydantic will still throw errors if we misuse a data type)
# pyright: reportGeneralTypeIssues=false


class QueryLiveStakeDistribution:
    """Ogmios method to query distribution of the stake across all known stake pools, relative to
    the total stake in the network.

    NOTE: This class is not intended to be used directly. Instead, use the
    Client.query_live_stake_distribution method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.queryLedgerState_liveStakeDistribution.value

    def execute(self, id: Optional[Any] = None) -> (dict, Optional[Any]):
        """Send and receive the request.

        :param id: The ID of the request.
        :type id: Any
        :return: A dict of stake distributions and ID of the response. Dict is of the format
            {<Blake2b_pool_id>: {"stake": stake_pct_str, "vrf": vrf_str}
        :rtype: (dict, Optional[Any])
        """
        self.send(id)
        return self.receive()

    def send(self, id: Optional[Any] = None) -> None:
        """Send the request.

        :param id: The ID of the request.
        :type id: Any
        """
        pld = om.QueryLedgerStateLiveStakeDistribution(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (dict, Optional[Any]):
        """Receive a previously requested response.

        :return: A dict of stake distributions and ID of the response. Dict is of the format
            {<Blake2b_pool_id>: {"stake": stake_pct_str, "vrf": vrf_str}
        :rtype: (dict, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_QueryLiveStakeDistribution_response(response)

    @staticmethod
    def _parse_QueryLiveStakeDistribution_response(response: dict) -> (dict, Optional[Any]):
        if response.get("method") != mm.Method.queryLedgerState_liveStakeDistribution.value:
            raise InvalidMethodError(
                f"Incorrect method for query_live_stake_distribution response: {response}"
            )

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        if stake_dist := response.get("result"):
            id: Optional[Any] = response.get("id")
            logger.info(
                f"""Parsed query_live_stake_distribution response:
        Stake distributed across {len(stake_dist.keys())} stake pools
        ID = {id}"""
            )
            return stake_dist, id
        raise InvalidResponseError(
            f"Failed to parse query_live_stake_distribution response: {response}"
        )
