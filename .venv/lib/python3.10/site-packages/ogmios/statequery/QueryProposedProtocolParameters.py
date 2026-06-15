from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ogmios.client import Client

from ogmios.errors import InvalidMethodError, InvalidResponseError, ResponseError
from ogmios.logger import logger
from ogmios.datatypes import ProtocolParameters
import ogmios.model.ogmios_model as om
import ogmios.model.model_map as mm

# pyright can't properly parse models, so we need to ignore its type checking
#  (pydantic will still throw errors if we misuse a data type)
# pyright: reportGeneralTypeIssues=false


class QueryProposedProtocolParameters:
    """Ogmios method to query the current protocol parameters.

    NOTE: This class is not intended to be used directly. Instead, use the
    Client.query_protocol_parameters method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.queryLedgerState_proposedProtocolParameters.value

    def execute(self, id: Optional[Any] = None) -> (ProtocolParameters, Optional[Any]):
        """Send and receive the request.

        :param id: The ID of the request.
        :type id: Any
        :return: Current protocol parameters.
        :rtype: (ProtocolParameters, Optional[Any])
        """
        self.send(id)
        return self.receive()

    def send(self, id: Optional[Any] = None) -> None:
        """Send the request.

        :param id: The ID of the request.
        :type id: Any
        """
        pld = om.QueryLedgerStateProposedProtocolParameters(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (ProtocolParameters, Optional[Any]):
        """Receive a previously requested response.

        :return: Current protocol parameters.
        :rtype: (ProtocolParameters, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_QueryProposedProtocolParameters_response(response)

    @staticmethod
    def _parse_QueryProposedProtocolParameters_response(
        response: dict,
    ) -> (ProtocolParameters, Optional[Any]):
        if response.get("method") != mm.Method.queryLedgerState_proposedProtocolParameters.value:
            raise InvalidMethodError(
                f"Incorrect method for query_proposed_protocol_parameters response: {response}"
            )

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        id: Optional[Any] = response.get("id")

        if response.get("result") == []:
            logger.info(f"""Parsed empty proposed_protocol_parameters with ID = {id}""")
            return None, id

        if result := response.get("result"):
            protocol_parameters = ProtocolParameters(**result[0])
            logger.info(
                f"""Parsed query_proposed_protocol_parameters response:
        Proposed Protocol Parameters = {protocol_parameters}
        ID = {id}"""
            )
            return protocol_parameters, id
        raise InvalidResponseError(
            f"Failed to parse query_proposed_protocol_parameters response: {response}"
        )
