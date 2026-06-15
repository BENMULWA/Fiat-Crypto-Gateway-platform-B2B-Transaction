from __future__ import annotations

from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from ogmios.client import Client

from ogmios.errors import InvalidMethodError, InvalidResponseError, ResponseError
from ogmios.logger import logger
from ogmios.datatypes import Origin, Point
import ogmios.response_handler as rh
import ogmios.model.ogmios_model as om
import ogmios.model.model_map as mm

# pyright can't properly parse models, so we need to ignore its type checking
#  (pydantic will still throw errors if we misuse a data type)
# pyright: reportGeneralTypeIssues=false


class AcquireLedgerState:
    """Ogmios method to acquire the ledger state at a given point.

    NOTE: This class is not intended to be used directly. Instead, use the
    Client.acquire_ledger_state method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.acquireLedgerState.value

    def execute(
        self, point: Union[Point, Origin], id: Optional[Any] = None
    ) -> Tuple[bool, Union[Point, Origin], Optional[Any]]:
        """Send and receive the request.

        :param point: The point at which to acquire the ledger state.
        :type point: Point | Origin
        :param id: The ID of the request.
        :type id: Any
        :return: The point or origin and ID of the response.
        :rtype: (Point | Origin, Optional[Any])
        """
        self.send(point, id)
        return self.receive()

    def send(self, point: Union[Point, Origin], id: Optional[Any] = None) -> None:
        """Send the request.

        :param point: The point at which to acquire the ledger state.
        :type point: Point | Origin
        :param id: The ID of the request.
        :type id: Any
        """
        params = om.Params3(point=point._schematype)
        pld = om.AcquireLedgerState(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            params=params,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> Tuple[Union[Point, Origin], Optional[Any]]:
        """Receive a previously requested response.

        :return: The point or origin and ID of the response.
        :rtype: (Point | Origin, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_AcquireLedgerState_response(response)

    @staticmethod
    def _parse_AcquireLedgerState_response(
        response: dict,
    ) -> Tuple[Union[Point, Origin], Optional[Any]]:
        if response.get("method") != mm.Method.acquireLedgerState.value:
            raise InvalidMethodError(
                f"Incorrect method for acquire_ledger_state response: {response}"
            )

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        # Successful response will contain success, point or origin, and ID
        if result := response.get("result"):
            if result.get("acquired") == "ledgerState" and result.get("point"):
                point: Union[Point, Origin] = rh.parse_PointOrOrigin(result.get("point"))
                id: Optional[Any] = response.get("id")
                logger.info(
                    f"""Parsed acquire_ledger_state response:
        Point = {point}
        ID = {id}"""
                )
                return point, id
        raise InvalidResponseError(f"Failed to parse acquire_ledger_state response: {response}")
