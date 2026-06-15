from __future__ import annotations

from typing import Any, Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ogmios.client import Client

from ogmios.errors import InvalidMethodError, InvalidResponseError, ResponseError
from ogmios.logger import logger
from ogmios.datatypes import EraSummary
import ogmios.model.ogmios_model as om
import ogmios.model.model_map as mm

# pyright can't properly parse models, so we need to ignore its type checking
#  (pydantic will still throw errors if we misuse a data type)
# pyright: reportGeneralTypeIssues=false


class QueryEraSummaries:
    """Ogmios method to query a summary of the slotting parameters and boundaries for each known
    era. Useful for doing slot-arithmetic and time conversions.

    NOTE: This class is not intended to be used directly. Instead, use the
    Client.query_era_summaries method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.queryLedgerState_eraSummaries.value

    def execute(self, id: Optional[Any] = None) -> (list[EraSummary], Optional[Any]):
        """Send and receive the request.

        :param id: The ID of the request.
        :type id: Any
        :return: List of era summaries and ID of the response.
        :rtype: (list[EraSummary], Optional[Any])
        """
        self.send(id)
        return self.receive()

    def send(self, id: Optional[Any] = None) -> None:
        """Send the request.

        :param id: The ID of the request.
        :type id: Any
        """
        pld = om.QueryLedgerStateEraSummaries(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (int, int, int, Optional[Any]):
        """Receive a previously requested response.

        :return: List of era summaries and ID of the response.
        :rtype: (list[eraSummary], Optional[Any])
        """
        response = self.client.receive()
        return self._parse_QueryEraSummaries_response(response)

    @staticmethod
    def _parse_QueryEraSummaries_response(response: dict) -> (list[EraSummary], Optional[Any]):
        if response.get("method") != mm.Method.queryLedgerState_eraSummaries.value:
            raise InvalidMethodError(
                f"Incorrect method for query_era_summaries response: {response}"
            )

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        if result := response.get("result"):
            summaries = []
            for era_summary in result:
                if not isinstance(era_summary, dict):
                    raise InvalidResponseError(
                        f"Failed to parse query_era_summaries response: {response}"
                    )
                if (
                    (start := era_summary.get("start")) is not None
                    and (end := era_summary.get("end")) is not None
                    and (parameters := era_summary.get("parameters")) is not None
                ):
                    summaries.append(
                        EraSummary(
                            start_time=start.get("time").get("seconds"),
                            start_slot=start.get("slot"),
                            start_epoch=start.get("epoch"),
                            end_time=end.get("time").get("seconds"),
                            end_slot=end.get("slot"),
                            end_epoch=end.get("epoch"),
                            epoch_length=parameters.get("epochLength"),
                            slot_length=parameters.get("slotLength").get("milliseconds"),
                            safe_zone=parameters.get("safeZone"),
                        )
                    )
            id: Optional[Any] = response.get("id")
            logger.info(
                f"""Parsed query_era_start response:
EraSummaries = {summaries}
ID = {id}"""
            )
            return summaries, id
        raise InvalidResponseError(f"Failed to parse query_era_summaries response: {response}")
