from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ogmios.client import Client

from ogmios.errors import InvalidMethodError, InvalidResponseError, ResponseError
from ogmios.logger import logger
from ogmios.datatypes import Origin, Point
import ogmios.model.ogmios_model as om
import ogmios.model.model_map as mm

# pyright can't properly parse models, so we need to ignore its type checking
#  (pydantic will still throw errors if we misuse a data type)
# pyright: reportGeneralTypeIssues=false


class QueryRewardAccountSummaries:
    """Ogmios method to query current delegation settings and rewards of
    chosen reward accounts.

    NOTE: This class is not intended to be used directly. Instead, use the
    Client.query_reward_account_summaries method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.queryLedgerState_rewardAccountSummaries.value

    def execute(
        self,
        scripts: list[str] = [],
        keys: list[str] = [],
        id: Optional[Any] = None,
    ) -> (dict, Optional[Any]):
        """Send and receive the request.

        :param scripts: The scripts to query - can be a base16/bech32 script hash or a stake address
        :type scripts: list[str]
        :param keys: The keys to query - can be a base16/bech32 stake key hash or a stake address
        :type keys: list[str]
        :param id: The ID of the request.
        :type id: Any
        :return: Current delegation settings and rewards of chosen reward accounts.
        :rtype: (dict, Optional[Any])
        """
        self.send(scripts, keys, id)
        return self.receive()

    def send(
        self,
        scripts: list[str] = [],
        keys: list[str] = [],
        id: Optional[Any] = None,
    ) -> None:
        """Send the request.

        :param scripts: The scripts to query - can be a base16/bech32 script hash or a stake address
        :type scripts: list[str]
        :param keys: The keys to query - can be a base16/bech32 stake key hash or a stake address
        :type keys: list[str]
        :param id: The ID of the request.
        :type id: Any
        """
        params = om.Params5(scripts=scripts, keys=keys)

        pld = om.QueryLedgerStateRewardAccountSummaries(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            params=params,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (dict, Optional[Any]):
        """Receive the response.

        :return: Current delegation settings and rewards of chosen reward accounts.
        :rtype: (dict, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_QueryRewardAccountSummaries_response(response)

    @staticmethod
    def _parse_QueryRewardAccountSummaries_response(
        response: dict,
    ) -> (dict, Optional[Any]):
        if response.get("method") != mm.Method.queryLedgerState_rewardAccountSummaries.value:
            raise InvalidMethodError(
                f"Incorrect method for query_reward_account_summaries response: {response}"
            )

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        id: Optional[Any] = response.get("id")

        if response.get("result") == {}:
            logger.info(f"Empty reward_account_summaries response (ID = {id}))")
            return {}, id

        if result := response.get("result"):
            logger.info(
                f"""Parsed reward_account_summaries response:
        Summaries = {result}
        ID = {id}"""
            )
            return result, id

        raise InvalidResponseError(
            f"Failed to parse query_reward_account_summaries response: {response}"
        )
