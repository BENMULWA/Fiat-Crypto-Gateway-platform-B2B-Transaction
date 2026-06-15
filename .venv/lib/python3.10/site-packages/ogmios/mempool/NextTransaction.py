from __future__ import annotations

from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from ogmios.client import Client

from ogmios.errors import InvalidMethodError, InvalidResponseError, ResponseError
from ogmios.logger import logger
import ogmios.model.ogmios_model as om
import ogmios.model.model_map as mm

# pyright can't properly parse models, so we need to ignore its type checking
#  (pydantic will still throw errors if we misuse a data type)
# pyright: reportGeneralTypeIssues=false


class NextTransaction:
    """Ogmios method to request the next mempool transaction from an acquired snapshot.

    NOTE: This class is not intended to be used directly. Instead, use the
    Client.next_transaction method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.nextTransaction.value

    def execute(self, id: Optional[Any] = None) -> Tuple[Union[None, str, dict], Optional[Any]]:
        """Send and receive the request.

        :param full_tx: If true, the full transaction will be returned. Otherwise, only the
            transaction ID will be returned.
        :type full_tx: bool
        :param id: The ID of the request.
        :type id: Any
        :return: The next mempool transaction from an acquired snapshot and ID of the response.
        :rtype: (dict, Optional[Any])
        """
        self.send(id)
        return self.receive()

    def send(self, id: Optional[Any] = None) -> None:
        """Send the request.

        :type full_tx: bool
        :param id: The ID of the request.
        :type id: Any
        """
        params = om.Params10(fields="all")
        pld = om.NextTransaction(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            params=params,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> Tuple[Union[None, dict], Optional[Any]]:
        """Receive a previously requested response.

        :return: The next mempool transaction from an acquired snapshot and ID of the response.
        :rtype: (dict, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_NextTransaction_response(response)

    @staticmethod
    def _parse_NextTransaction_response(
        response: dict,
    ) -> Tuple[Union[None, dict], Optional[Any]]:
        if response.get("method") != mm.Method.nextTransaction.value:
            raise InvalidMethodError(f"Incorrect method for next_transaction response: {response}")

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        if (result := response.get("result")) is not None:
            id: Optional[Any] = response.get("id")
            tx_rsp = result.get("transaction")
            if tx_rsp is None:
                return None, id
            tx = tx_rsp
            logger.info(
                f"""Parsed next_transaction response:
        TX = {tx}
        ID = {id}"""
            )
            return tx, id
        raise InvalidResponseError(f"Failed to parse next_transaction response: {response}")
