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


class SubmitTransaction:
    """Ogmios method to submit a signed and serialized transaction to the network.

    NOTE: This class is not intended to be used directly. Instead, use the
    Client.submit_transaction method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.submitTransaction.value

    def execute(self, tx_cbor: str, id: Optional[Any] = None) -> (str, Optional[Any]):
        """Send and receive the request.

        :param tx_id: CBOR serialized transaction to submit.
        :type tx_id: str
        :param id: The ID of the request.
        :type id: Any
        :return: The submitted transaction's ID and ID of the response.
        :rtype: (str, Optional[Any])
        """
        self.send(tx_cbor, id)
        return self.receive()

    def send(self, tx_cbor: str, id: Optional[Any] = None) -> None:
        """Send the request.

        :param tx_id: CBOR serialized transaction to submit.
        :type tx_id: str
        :param id: The ID of the request.
        :type id: Any
        """
        params = om.Params1(transaction=om.Transaction(cbor=tx_cbor))
        pld = om.SubmitTransaction(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            params=params,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (str, Optional[Any]):
        """Receive a previously requested response.

        :return: The submitted transaction's ID and ID of the response.
        :rtype: (str, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_SubmitTransaction_response(response)

    @staticmethod
    def _parse_SubmitTransaction_response(
        response: dict,
    ) -> (str, Optional[Any]):
        if response.get("method") != mm.Method.submitTransaction.value:
            raise InvalidMethodError(
                f"Incorrect method for submit_transaction response: {response}"
            )

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        if (result := response.get("result")) is not None:
            id: Optional[Any] = response.get("id")
            tx_id = result.get("transaction").get("id")
            logger.info(
                f"""Parsed submit_transaction response:
        TX ID = {tx_id}
        ID = {id}"""
            )
            return tx_id, id
        raise InvalidResponseError(f"Failed to parse submit_transaction response: {response}")
