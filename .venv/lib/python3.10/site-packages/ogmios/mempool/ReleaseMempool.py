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


class ReleaseMempool:
    """Ogmios method to release a mempool snapshot.

    NOTE: This class is not intended to be used directly. Instead, use the Client.release_mempool
    method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.releaseMempool.value

    def execute(self, id: Optional[Any] = None) -> Optional[Any]:
        """Send and receive the request.

        :param id: The ID of the request.
        :type id: Any
        :return: The ID of the response.
        :rtype: Optional[Any]
        """
        self.send(id)
        return self.receive()

    def send(self, id: Optional[Any] = None) -> None:
        """Send the request.

        :param id: The ID of the request.
        :type id: Any
        """
        pld = om.ReleaseMempool(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (int, Optional[Any]):
        """Receive a previously requested response.

        :return: The ID of the response.
        :rtype: Optional[Any]
        """
        response = self.client.receive()
        return self._parse_ReleaseMempool_response(response)

    @staticmethod
    def _parse_ReleaseMempool_response(response: dict) -> Optional[Any]:
        if response.get("method") != mm.Method.releaseMempool.value:
            raise InvalidMethodError(f"Incorrect method for release_mempool response: {response}")

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        if response.get("result") != {} and response.get("result").get("released") == "mempool":
            id: Optional[Any] = response.get("id")
            logger.info(
                f"""Parsed release_mempool response:
        ID = {id}"""
            )
            return id
        raise InvalidResponseError(f"Failed to parse release_mempool response: {response}")
