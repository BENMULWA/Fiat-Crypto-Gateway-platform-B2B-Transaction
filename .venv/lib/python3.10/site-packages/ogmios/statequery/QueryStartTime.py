from __future__ import annotations

from typing import Any, Optional
from typing import TYPE_CHECKING
from datetime import datetime

if TYPE_CHECKING:
    from ogmios.client import Client

from ogmios.errors import InvalidMethodError, InvalidResponseError, ResponseError
from ogmios.logger import logger
import ogmios.model.ogmios_model as om
import ogmios.model.model_map as mm

# pyright can't properly parse models, so we need to ignore its type checking
#  (pydantic will still throw errors if we misuse a data type)
# pyright: reportGeneralTypeIssues=false


class QueryStartTime:
    """Ogmios method to query the chain's start time (UTC).

    NOTE: This class is not intended to be used directly. Instead, use the Client.query_start_time
    method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.queryNetwork_startTime.value

    def execute(self, id: Optional[Any] = None) -> (datetime, Optional[Any]):
        """Send and receive the request.

        :param id: The ID of the request.
        :type id: Any
        :return: The chain start time (UTC) and ID of the response.
        :rtype: (datetime, Optional[Any])
        """
        self.send(id)
        return self.receive()

    def send(self, id: Optional[Any] = None) -> None:
        """Send the request.

        :param id: The ID of the request.
        :type id: Any
        """
        pld = om.QueryNetworkStartTime(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (datetime, Optional[Any]):
        """Receive a previously requested response.

        :return: The chain start time (UTC) and ID of the response.
        :rtype: (datetime, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_QueryStartTime_response(response)

    @staticmethod
    def _parse_QueryStartTime_response(response: dict) -> (datetime, Optional[Any]):
        if response.get("method") != mm.Method.queryNetwork_startTime.value:
            raise InvalidMethodError(f"Incorrect method for query_start_time response: {response}")

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        if result := response.get("result"):
            start_time: datetime = datetime.strptime(result, "%Y-%m-%dT%H:%M:%SZ")
            id: Optional[Any] = response.get("id")
            logger.info(
                f"""Parsed query_start_time response:
        Start Time = {start_time}
        ID = {id}"""
            )
            return start_time, id
        raise InvalidResponseError(f"Failed to parse query_start_time response: {response}")
