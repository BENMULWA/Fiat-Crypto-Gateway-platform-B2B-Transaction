from __future__ import annotations

from typing import Any, Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ogmios.client import Client

from ogmios.errors import InvalidMethodError, InvalidResponseError, ResponseError
from ogmios.logger import logger
from ogmios.datatypes import GenesisConfiguration, Era
import ogmios.model.ogmios_model as om
import ogmios.model.model_map as mm

# pyright can't properly parse models, so we need to ignore its type checking
#  (pydantic will still throw errors if we misuse a data type)
# pyright: reportGeneralTypeIssues=false


class QueryGenesisConfiguration:
    """Ogmios method to query the genesis configuration of a specific era.

    NOTE: This class is not intended to be used directly. Instead, use the
    Client.query_genesis_configuration method.

    :param client: The client to use for the request.
    :type client: Client
    """

    def __init__(self, client: Client):
        self.client = client
        self.method = mm.Method.queryNetwork_genesisConfiguration.value

    def execute(
        self, era: mm.Era, id: Optional[Any] = None
    ) -> (GenesisConfiguration, Optional[Any]):
        """Send and receive the request.

        :param era: The era at which to query the genesis configuration.
        :type era: mm.Era
        :param id: The ID of the request.
        :type id: Any
        :return: The GenesisConfiguration at the specified era and ID of the response.
        :rtype: (GenesisConfiguration, Optional[Any])
        """
        self.send(era, id)
        return self.receive()

    def send(self, era: mm.Era, id: Optional[Any] = None) -> None:
        """Send the request.

        :param era: The era at which to query the genesis configuration.
        :type era: mm.Era
        :param id: The ID of the request.
        :type id: Any
        """
        params = om.Params9(era=era)
        pld = om.QueryNetworkGenesisConfiguration(
            jsonrpc=self.client.rpc_version,
            method=self.method,
            params=params,
            id=id,
        )
        self.client.send(pld.json())

    def receive(self) -> (GenesisConfiguration, Optional[Any]):
        """Receive a previously requested response.

        :return: The GenesisConfiguration at the specified era and ID of the response.
        :rtype: (GenesisConfiguration, Optional[Any])
        """
        response = self.client.receive()
        return self._parse_QueryGenesisConfiguration_response(response)

    @staticmethod
    def _parse_QueryGenesisConfiguration_response(
        response: dict,
    ) -> (GenesisConfiguration, Optional[Any]):
        if response.get("method") != mm.Method.queryNetwork_genesisConfiguration.value:
            raise InvalidMethodError(
                f"Incorrect method for query_genesis_configuration response: {response}"
            )

        if response.get("error"):
            raise ResponseError(f"Ogmios responded with error: {response}")

        # Successful response will contain success, point or origin, and ID
        if result := response.get("result"):
            genesis_configuration = None
            if (era := result.get("era")) is not None:
                if (
                    era == Era.byron.value
                    and (genesis_key_hashes := result.get("genesisKeyHashes")) is not None
                    and (genesis_delegations := result.get("genesisDelegations")) is not None
                    and (start_time := result.get("startTime")) is not None
                    and (initial_funds := result.get("initialFunds")) is not None
                    and (initial_vouchers := result.get("initialVouchers")) is not None
                    and (security_parameter := result.get("securityParameter")) is not None
                    and (network_magic := result.get("networkMagic")) is not None
                    and (updatable_parameters := result.get("updatableParameters")) is not None
                ):
                    genesis_configuration = GenesisConfiguration(
                        era=era,
                        genesis_key_hashes=genesis_key_hashes,
                        genesis_delegations=genesis_delegations,
                        start_time=start_time,
                        initial_funds=initial_funds,
                        initial_vouchers=initial_vouchers,
                        security_parameter=security_parameter,
                        network_magic=network_magic,
                        updatable_parameters=updatable_parameters,
                    )
                elif (
                    era == Era.shelley.value
                    and (start_time := result.get("startTime")) is not None
                    and (network_magic := result.get("networkMagic")) is not None
                    and (network := result.get("network")) is not None
                    and (active_slots_coefficient := result.get("activeSlotsCoefficient"))
                    is not None
                    and (security_parameter := result.get("securityParameter")) is not None
                    and (epoch_length := result.get("epochLength")) is not None
                    and (slots_per_kes_period := result.get("slotsPerKesPeriod")) is not None
                    and (max_kes_evolutions := result.get("maxKesEvolutions")) is not None
                    and (slot_length := result.get("slotLength")) is not None
                    and (update_quorum := result.get("updateQuorum")) is not None
                    and (max_lovelace_supply := result.get("maxLovelaceSupply")) is not None
                    and (initial_parameters := result.get("initialParameters")) is not None
                    and (initial_delegates := result.get("initialDelegates")) is not None
                    and (initial_funds := result.get("initialFunds")) is not None
                    and (initial_stake_pools := result.get("initialStakePools")) is not None
                ):
                    genesis_configuration = GenesisConfiguration(
                        era=era,
                        start_time=start_time,
                        network_magic=network_magic,
                        network=network,
                        active_slots_coefficient=active_slots_coefficient,
                        security_parameter=security_parameter,
                        epoch_length=epoch_length,
                        slots_per_kes_period=slots_per_kes_period,
                        max_kes_evolutions=max_kes_evolutions,
                        slot_length=slot_length,
                        update_quorum=update_quorum,
                        max_lovelace_supply=max_lovelace_supply,
                        initial_parameters=initial_parameters,
                        initial_delegates=initial_delegates,
                        initial_funds=initial_funds,
                        initial_stake_pools=initial_stake_pools,
                    )
                elif (
                    era == Era.alonzo.value
                    and (updatable_parameters := result.get("updatableParameters")) is not None
                ):
                    genesis_configuration = GenesisConfiguration(
                        era=era, updatableParameters=updatable_parameters
                    )
                elif (
                    era == Era.conway.value
                    and (constitution := result.get("constitution")) is not None
                    and (constitutional_committee := result.get("constitutionalCommittee"))
                    is not None
                    and (updatable_parameters := result.get("updatableParameters")) is not None
                ):
                    genesis_configuration = GenesisConfiguration(
                        era=era,
                        constitution=constitution,
                        constitutional_committee=constitutional_committee,
                        updatableParameters=updatable_parameters,
                    )

            if genesis_configuration:
                id: Optional[Any] = response.get("id")
                logger.info(
                    f"""Parsed query_genesis_configuration response:
        Configuration = {genesis_configuration}
        ID = {id}"""
                )
                return genesis_configuration, id
        raise InvalidResponseError(
            f"Failed to parse query_genesis_configuration response: {response}"
        )
