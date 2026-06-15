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


class HasTransaction:
    """Ogmios method to ask whether a given transaction is present in the acquired
    mempool snapshot.

    NOTE: This class is not intended to be used directly. Instead, use the
    Client.has_transaction method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.hasTransaction.value

    def execute(self, tx_id: str, id: Optional[Any] = None) -> (bool, Optional[Any]):
        """Send and receive the request.

        :param tx_id: The ID of the transaction to check for.
        :type tx_id: str
        :param id: The ID of the request.
        :type id: Any
        :return: Whether the transaction is present in the mempool snapshot and ID of the response.
        :rtype: (bool, Optional[Any])
        """
        self.send(tx_id, id)
        return self.receive()

    def send(self, tx_id: str, id: Optional[Any] = None) -> None:
        """Send the request.

        :param tx_id: The ID of the transaction to check for.
        :type tx_id: str
        :param id: The ID of the request.
        :type id: Any
        """
        params = om.Params11(id=tx_id)
        pld = om.HasTransaction(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            params=params,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (bool, Optional[Any]):
        """Receive a previously requested response.

        :return: Whether the transaction is present in the mempool snapshot and ID of the response.
        :rtype: (bool, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_HasTransaction_response(response)

    @staticmethod
    def _parse_HasTransaction_response(
        response: dict,
    ) -> (bool, Optional[Any]):
        if response.get("method") != mm.Method.hasTransaction.value:
            raise InvalidMethodError(f"Incorrect method for has_transaction response: {response}")

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        if (has_tx := response.get("result")) is not None:
            id: Optional[Any] = response.get("id")
            logger.info(
                f"""Parsed has_transaction response:
        Has TX = {has_tx}
        ID = {id}"""
            )
            return has_tx, id
        raise InvalidResponseError(f"Failed to parse has_transaction response: {response}")
