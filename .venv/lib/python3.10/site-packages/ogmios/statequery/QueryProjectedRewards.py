from __future__ import annotations

from typing import Any, Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ogmios.client import Client

from ogmios.errors import InvalidMethodError, InvalidResponseError, ResponseError
from ogmios.logger import logger
from ogmios.datatypes import Utxo, Ada
import ogmios.model.ogmios_model as om
import ogmios.model.model_map as mm

# pyright can't properly parse models, so we need to ignore its type checking
#  (pydantic will still throw errors if we misuse a data type)
# pyright: reportGeneralTypeIssues=false


class QueryProjectedRewards:
    """Ogmios method to query the projected rewards of an account in a context where the top
    stake pools are fully saturated. This projection gives, in principle, a ranking of stake
    pools that maximizes delegator rewards.

    NOTE: This class is not intended to be used directly. Instead, use the
    Client.query_projected_rewards method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.queryLedgerState_projectedRewards.value

    def execute(
        self,
        stake: Ada,
        scripts: list[str] = [],
        keys: list[str] = [],
        id: Optional[Any] = None,
    ) -> (dict, Optional[Any]):
        """Send and receive the request.

        :param stake: The amount of staked lovelace to query
        :type stake: Ada
        :param scripts: The scripts to query - can be a base16/bech32 script hash or a stake address
        :type scripts: list[str]
        :param keys: The keys to query - can be a base16/bech32 stake key hash or a stake address
        :type keys: list[str]
        :param id: The ID of the request.
        :type id: Any
        :return: Rewards that can be expected assuming a pool is fully saturated. Such rewards are
            said non-myopic, in opposition to short-sighted rewards looking at immediate benefits.
            Keys of the map can be either Ada amounts or account credentials depending on the
            query. Additionally returns the ID of the response.
        :rtype: (dict, Optional[Any])
        """
        self.send(stake, scripts, keys, id)
        return self.receive()

    def send(
        self,
        stake: list[Ada],
        scripts: list[str] = [],
        keys: list[str] = [],
        id: Optional[Any] = None,
    ) -> None:
        """Send the request.

        :param stake: The amounts of staked lovelace to query
        :type stake: list[Ada]
        :param scripts: The scripts to query - can be a base16/bech32 script hash or a stake address
        :type scripts: list[str]
        :param keys: The keys to query - can be a base16/bech32 stake key hash or a stake address
        :type keys: list[str]
        :param id: The ID of the request.
        :type id: Any
        """
        params = om.Params4(stake=[amt.__dict__() for amt in stake], scripts=scripts, keys=keys)

        pld = om.QueryLedgerStateProjectedRewards(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            params=params,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (dict, Optional[Any]):
        """Receive the response.

        :return: Rewards that can be expected assuming a pool is fully saturated. Such rewards are
            said non-myopic, in opposition to short-sighted rewards looking at immediate benefits.
            Keys of the map can be either Ada amounts or account credentials depending on the
            query. Additionally returns the ID of the response.
        :rtype: (dict, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_QueryProjectedRewards_response(response)

    @staticmethod
    def _parse_QueryProjectedRewards_response(
        response: dict,
    ) -> (dict, Optional[Any]):
        if response.get("method") != mm.Method.queryLedgerState_projectedRewards.value:
            raise InvalidMethodError(
                f"Incorrect method for query_projected_rewards response: {response}"
            )

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        if result := response.get("result"):
            id: Optional[Any] = response.get("id")
            logger.info(
                f"""Parsed projected_rewards response:
        Projection = {result}
        ID = {id}"""
            )
            return result, id

        raise InvalidResponseError(f"Failed to parse query_projected_rewards response: {response}")
