from __future__ import annotations

from typing import Any, Optional, Tuple, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from ogmios.client import Client

from ogmios.errors import InvalidMethodError, InvalidResponseError, ResponseError
from ogmios.logger import logger
from ogmios.datatypes import Origin
import ogmios.response_handler as rh
import ogmios.model.ogmios_model as om
import ogmios.model.model_map as mm

# pyright can't properly parse models, so we need to ignore its type checking
#  (pydantic will still throw errors if we misuse a data type)
# pyright: reportGeneralTypeIssues=false


class QueryBlockHeight:
    """Ogmios method to query the chain's highest block number.

    NOTE: This class is not intended to be used directly. Instead, use the
    Client.query_block_height method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.queryNetwork_blockHeight.value

    def execute(self, id: Optional[Any] = None) -> Tuple[Union[int, Origin], Optional[Any]]:
        """Send and receive the request.

        :param id: The ID of the request.
        :type id: Any
        :return: The block height or origin and ID of the response.
        :rtype: (int | Origin, Optional[Any])
        """
        self.send(id)
        return self.receive()

    def send(self, id: Optional[Any] = None) -> None:
        """Send the request.

        :param id: The ID of the request.
        :type id: Any
        """
        pld = om.QueryNetworkBlockHeight(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> Tuple[Union[int, Origin], Optional[Any]]:
        """Receive a previously requested response.

        :return: The block height or origin and ID of the response.
        :rtype: (int | Origin, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_QueryBlockHeight_response(response)

    @staticmethod
    def _parse_QueryBlockHeight_response(
        response: dict,
    ) -> Tuple[Union[int, Origin], Optional[Any]]:
        if response.get("method") != mm.Method.queryNetwork_blockHeight.value:
            raise InvalidMethodError(
                f"Incorrect method for query_block_height response: {response}"
            )

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        # Successful response will contain block height or origin, and ID
        if result := response.get("result"):
            block_height: Union[int, Origin] = rh.parse_BlockHeightOrOrigin(result)
            id: Optional[Any] = response.get("id")
            logger.info(
                f"""Parsed query_block_height response:
        Block Height = {block_height}
        ID = {id}"""
            )
            return block_height, id
        raise InvalidResponseError(f"Failed to parse query_block_height response: {response}")
