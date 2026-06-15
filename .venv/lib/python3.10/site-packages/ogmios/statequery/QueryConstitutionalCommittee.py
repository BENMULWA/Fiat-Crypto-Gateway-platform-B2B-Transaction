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


class QueryConstitutionalCommittee:
    """Ogmios method to query the on-chain constitutional committee.

    NOTE: This class is not intended to be used directly. Instead, use the Client.query_constitutional_committee
    method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.queryLedgerState_constitutionalCommittee.value

    def execute(self, id: Optional[Any] = None) -> (dict, Optional[Any]):
        """Send and receive the request.

        :param id: The ID of the request.
        :type id: Any
        :return: The constitutional committee and ID of the response.
        :rtype: (int, Optional[Any])
        """
        self.send(id)
        return self.receive()

    def send(self, id: Optional[Any] = None) -> None:
        """Send the request.

        :param id: The ID of the request.
        :type id: Any
        """
        pld = om.QueryLedgerStateConstitutionalCommittee(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (dict, Optional[Any]):
        """Receive a previously requested response.

        :return: The constitutional committee and ID of the response.
        :rtype: (int, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_QueryConstitutionalCommittee_response(response)

    @staticmethod
    def _parse_QueryConstitutionalCommittee_response(response: dict) -> (dict, Optional[Any]):
        if response.get("method") != mm.Method.queryLedgerState_constitutionalCommittee.value:
            raise InvalidMethodError(
                f"Incorrect method for query_constitutional_committee response: {response}"
            )

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        # Successful response will contain block height or origin, and ID
        if result := response.get("result"):
            id: Optional[Any] = response.get("id")
            logger.info(
                f"""Parsed query_constitutional_committee response:
        Result = {result}
        ID = {id}"""
            )
            return result, id
        raise InvalidResponseError(
            f"Failed to parse query_constitutional_committee response: {response}"
        )
