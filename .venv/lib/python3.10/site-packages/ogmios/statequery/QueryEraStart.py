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


class QueryEraStart:
    """Ogmios method to query information regarding the beginning of the ledger's current era.

    NOTE: This class is not intended to be used directly. Instead, use the Client.query_era_start
    method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.queryLedgerState_eraStart.value

    def execute(self, id: Optional[Any] = None) -> (int, int, int, Optional[Any]):
        """Send and receive the request.

        :param id: The ID of the request.
        :type id: Any
        :return: The era's start time (in seconds, relative to the network start), slot, epoch, and
            ID of the response.
        :rtype: (int, int, int, Optional[Any])
        """
        self.send(id)
        return self.receive()

    def send(self, id: Optional[Any] = None) -> None:
        """Send the request.

        :param id: The ID of the request.
        :type id: Any
        """
        pld = om.QueryLedgerStateEraStart(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (int, int, int, Optional[Any]):
        """Receive a previously requested response.

        :return: The era's start time (in seconds, relative to the network start), slot, epoch, and
            ID of the response.
        :rtype: (int, int, int, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_QueryEraStart_response(response)

    @staticmethod
    def _parse_QueryEraStart_response(response: dict) -> (int, int, int, Optional[Any]):
        if response.get("method") != mm.Method.queryLedgerState_eraStart.value:
            raise InvalidMethodError(f"Incorrect method for query_era_start response: {response}")

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        if result := response.get("result"):
            if (
                ((time := result.get("time")) is not None)
                and ((slot := result.get("slot")) is not None)
                and ((epoch := result.get("epoch")) is not None)
            ):
                time: int = time.get("seconds")
                id: Optional[Any] = response.get("id")
                logger.info(
                    f"""Parsed query_era_start response:
        Epoch = {epoch}
        ID = {id}"""
                )
                return time, slot, epoch, id
        raise InvalidResponseError(f"Failed to parse query_era_start response: {response}")
