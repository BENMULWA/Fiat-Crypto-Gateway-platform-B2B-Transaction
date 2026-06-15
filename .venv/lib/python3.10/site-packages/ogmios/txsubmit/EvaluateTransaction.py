from __future__ import annotations

from typing import Any, Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ogmios.client import Client

from ogmios.errors import InvalidMethodError, InvalidResponseError, ResponseError
from ogmios.logger import logger
from ogmios.datatypes import Utxo
import ogmios.model.ogmios_model as om
import ogmios.model.model_map as mm

# pyright can't properly parse models, so we need to ignore its type checking
#  (pydantic will still throw errors if we misuse a data type)
# pyright: reportGeneralTypeIssues=false


class EvaluateTransaction:
    """Ogmios method to evaluate execution units of scripts in a well-formed transaction.

    NOTE: This class is not intended to be used directly. Instead, use the
    Client.evaluate_transaction method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.evaluateTransaction.value

    def execute(
        self, tx_cbor: str, additional_utxo: Optional[Utxo] = None, id: Optional[Any] = None
    ) -> (dict, Optional[Any]):
        """Send and receive the request.

        :param tx_id: CBOR serialized transaction to evaluate.
        :type tx_id: str
        :param additional_utxo: Additional UTxO to include in the transaction evaluation.
        :type additional_utxo: Optional[Utxo]
        :param id: The ID of the request.
        :type id: Any
        :return: The TX's execution units and ID of the response.
        :rtype: (dict, Optional[Any])
        """
        self.send(tx_cbor, additional_utxo, id)
        return self.receive()

    def send(
        self, tx_cbor: str, additional_utxo: Optional[Utxo] = None, id: Optional[Any] = None
    ) -> None:
        """Send the request.

        :param tx_id: CBOR serialized transaction to evaluate.
        :type tx_id: str
        :param additional_utxo: Additional UTxO to include in the transaction evaluation.
        :type additional_utxo: Optional[Utxo]
        :param id: The ID of the request.
        :type id: Any
        """
        params = om.Params2(
            transaction=om.Transaction(cbor=tx_cbor), additionalUtxo=additional_utxo
        )
        pld = om.EvaluateTransaction(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            params=params,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (dict, Optional[Any]):
        """Receive a previously requested response.

        :return: The TX's execution units and ID of the response.
        :rtype: (dict, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_EvaluateTransaction_response(response)

    @staticmethod
    def _parse_EvaluateTransaction_response(
        response: dict,
    ) -> (dict, Optional[Any]):
        if response.get("method") != mm.Method.evaluateTransaction.value:
            raise InvalidMethodError(
                f"Incorrect method for evaluate_transaction response: {response}"
            )

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        if (result := response.get("result")) is not None:
            id: Optional[Any] = response.get("id")
            logger.info(
                f"""Parsed evaluate_transaction response:
        Evaluation = {result}
        ID = {id}"""
            )
            return result, id
        raise InvalidResponseError(f"Failed to parse evaluate_transaction response: {response}")
