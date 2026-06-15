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


class QueryStakePools:
    """Ogmios method to list of all stake pool identifiers currently registered and active.

    NOTE: This class is not intended to be used directly. Instead, use the Client.query_stake_pools
    method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.queryLedgerState_stakePools.value

    def execute(self, stake_pools: list[str], id: Optional[Any] = None) -> (dict, Optional[Any]):
        """Send and receive the request.

        :param stake_pools: The list of stake pool bech32 IDs to query.
        :type points: list[str]
        :param id: The ID of the request.
        :type id: Any
        :return: Dict of stake pool summaries and ID of the response.
        :rtype: (dict, Optional[Any])
        """
        self.send(stake_pools, id)
        return self.receive()

    def send(self, stake_pools: list[str], id: Optional[Any] = None) -> None:
        """Send the request.

        :param stake_pools: The list of stake pool bech32 IDs to query.
        :type points: list[str]
        :param id: The ID of the request.
        :type id: Any
        """
        params = om.Params6(stakePools=[om.StakePool(id=stake_pool) for stake_pool in stake_pools])
        pld = om.QueryLedgerStateStakePools(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            params=params,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (dict, Optional[Any]):
        """Receive the response.

        :return: Dict of stake pool summaries and ID of the response.
        :rtype: (dict, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_QueryStakePools_response(response)

    @staticmethod
    def _parse_QueryStakePools_response(
        response: dict,
    ) -> (dict, Optional[Any]):
        if response.get("method") != mm.Method.queryLedgerState_stakePools.value:
            raise InvalidMethodError(f"Incorrect method for query_stake_pool response: {response}")

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        if result := response.get("result"):
            id: Optional[Any] = response.get("id")
            logger.info(
                f"""Parsed query_stake_pool response:
        Stake Pool Summaries = {result}
        ID = {id}"""
            )
            return result, id

        raise InvalidResponseError(f"Failed to parse query_stake_pool response: {response}")
