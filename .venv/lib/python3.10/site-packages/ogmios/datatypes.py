"""This module contains user-friendly datatypes for Ogmios objects.

    Behind the scenes, ogmios-python uses objects derived from the cardano.json and ogmios.json
    schema files provided by Ogmios. These types are useful for validating that data passed to
    and from the Ogmios server is properly formatted. However, the schema types are not
    conducive to Pythonic use. Therefore, we use intermediate datatypes for user-facing
    functions of this library.

    .. seealso::
        Cardano schema: `cardano.json <https://ogmios.dev/cardano.json>`_,
        Ogmios schema: `ogmios.json schema <https://ogmios.dev/ogmios.json>`_

"""

from datetime import datetime
from enum import Enum
from typing import Dict, Union
from fractions import Fraction

from ogmios.errors import InvalidOgmiosParameter
import ogmios.model.cardano_model as cm
import ogmios.model.ogmios_model as om
import ogmios.model.model_map as mm

# pyright can't properly parse models, so we need to ignore its type checking
#  (pydantic will still throw errors if we misuse a data type)
# pyright: reportGeneralTypeIssues=false


class Era(Enum):
    """An enum representing the eras of the Cardano blockchain."""

    byron = "byron"
    shelley = "shelley"
    allegra = "allegra"
    mary = "mary"
    alonzo = "alonzo"
    babbage = "babbage"
    conway = "conway"

    @staticmethod
    def by_index(index):
        return list(Era)[index]

    @staticmethod
    def get_index(era):
        return list(Era).index(era)

    @staticmethod
    def is_genesis_era(era):
        return era in [Era.byron, Era.shelley, Era.alonzo, Era.conway]


class Direction(Enum):
    """An enum representing the direction of a blockchain traversal."""

    forward = "forward"
    backward = "backward"


class Origin:
    """A class representing the origin of the blockchain.

    The origin is the first block in the blockchain. It is the only block that does not have a
    parent block.
    """

    def __init__(self):
        self._schematype = om.Origin.origin

    def __eq__(self, other):
        return True if isinstance(other, Origin) else False

    def __str__(self):
        return "Origin()"


class Point:
    """A class representing a point in the blockchain.

    :param slot: The slot number of the point.
    :type slot: int
    :param id: The block hash of the point.
    :type id: str
    """

    def __init__(self, slot: int, id: str):
        self.slot = slot
        self.id = id
        self._schematype = om.PointOrOrigin1(slot=self.slot, id=self.id)

    def __eq__(self, other):
        if isinstance(other, Point):
            if self.slot == other.slot and self.id == other.id:
                return True
        return False

    def __str__(self):
        return f"Point(Slot={self.slot:,}, ID={self.id})"


class Tip:
    """A class representing the tip of the blockchain.

    :param slot: The slot number of the tip.
    :type slot: int
    :param id: The block hash of the tip.
    :type id: str
    :param height: The block height of the tip.
    :type height: int
    """

    def __init__(self, slot: int, id: str, height: int):
        self.slot = slot
        self.id = id
        self.height = height
        self._schematype = om.Tip(slot=self.slot, id=self.id, height=self.height)

    def __eq__(self, other):
        if isinstance(other, Tip):
            if self.slot == other.slot and self.id == other.id and self.height == other.height:
                return True
        return False

    def __str__(self):
        return f"Tip(Slot={self.slot:,}, Height={self.height:,}, ID={self.id})"

    def to_point(self) -> Point:
        """Returns a Point representation of the Tip"""
        return Point(self.slot, self.id)


class Block:
    """
    Represents a block in the blockchain.

    :param blocktype: The type of the block (EBB, BFT, or Praos)
    :type blocktype: ogmios.model.model_map.Types
    :param kwargs: Additional arguments depending on the block type.
    :raises InvalidOgmiosParameter: If an unsupported block type is provided.
    """

    def __init__(self, blocktype: mm.Types, **kwargs):
        self.blocktype = blocktype
        if blocktype == mm.Types.ebb.value:
            self.era = kwargs.get("era")
            self.id = kwargs.get("id")
            self.ancestor = kwargs.get("ancestor")
            self.height = kwargs.get("height")
            self._schematype = cm.BlockEBB(
                type=self.blocktype,
                era=self.era,
                id=self.id,
                ancestor=self.ancestor,
                height=self.height,
            )
        elif blocktype == mm.Types.bft.value:
            self.era = kwargs.get("era")
            self.id = kwargs.get("id")
            self.ancestor = kwargs.get("ancestor")
            self.height = kwargs.get("height")
            self.slot = kwargs.get("slot")
            self.size = kwargs.get("size")
            self.transactions = kwargs.get("transactions")
            self.protocol = kwargs.get("protocol")
            self.issuer = kwargs.get("issuer")
            self.delegate = kwargs.get("delegate")
            self._schematype = cm.BlockBFT(
                type=self.blocktype,
                era=self.era,
                id=self.id,
                ancestor=self.ancestor,
                height=self.height,
                slot=self.slot,
                size=self.size,
                transactions=self.transactions,
                protocol=self.protocol,
                issuer=self.issuer,
                delegate=self.delegate,
            )
        elif blocktype == mm.Types.praos.value:
            self.era = kwargs.get("era")
            self.id = kwargs.get("id")
            self.ancestor = kwargs.get("ancestor")
            self.nonce = kwargs.get("nonce")
            self.height = kwargs.get("height")
            self.slot = kwargs.get("slot")
            self.size = kwargs.get("size")
            self.transactions = kwargs.get("transactions")
            self.protocol = kwargs.get("protocol")
            self.issuer = kwargs.get("issuer")
            self._schematype = cm.BlockPraos(
                type=self.blocktype,
                era=self.era,
                id=self.id,
                ancestor=self.ancestor,
                height=self.height,
                slot=self.slot,
                size=self.size,
                protocol=self.protocol,
                issuer=self.issuer,
            )
        else:
            raise InvalidOgmiosParameter(f"Unsupported block type: {blocktype}")

    def __str__(self):
        """
        Returns a string representation of the block.

        :return: A string representation of the block.
        :rtype: str
        """
        if self.blocktype == mm.Types.ebb.value:
            return f"Block(Type=EBB, Era={self.era}, ID={self.id}, Ancestor={self.ancestor}, Height={self.height:,})"
        elif self.blocktype == mm.Types.bft.value:
            return f"Block(Type=BFT, Era={self.era}, ID={self.id}, Ancestor={self.ancestor}, Height={self.height:,}, Slot={self.slot:,}, Size={self.size.get('bytes'):,}, TXs={len(self.transactions)})"
        elif self.blocktype == mm.Types.praos.value:
            return f"Block(Type=Praos, Era={self.era}, ID={self.id}, Ancestor={self.ancestor}, Height={self.height:,}, Slot={self.slot:,}, Size={self.size.get('bytes'):,}, TXs={len(self.transactions)})"

    def __eq__(self, other):
        if isinstance(other, Block):
            if (
                self.blocktype == other.blocktype
                and self.era == other.era
                and self.id == other.id
                and self.ancestor == other.ancestor
                and self.height == other.height
                and self.slot == other.slot
                and self.size == other.size
                and self.transactions == other.transactions
                and self.protocol == other.protocol
                and self.issuer == other.issuer
                and self.delegate == other.delegate
            ):
                return True
        return False

    def to_point(self) -> Point:
        """Returns a Point representation of the Block"""
        return Point(self.slot, self.id)

    def to_tip(self) -> Tip:
        """Returns a Tip representation of the Block"""
        return Tip(self.slot, self.id, self.height)


class Script:
    """A class representing a script.

    :param language: The language of the script.
    :type language: str
    :param cbor: The CBOR representation of the script.
    :type cbor: str
    :param json: The JSON representation of the script.
    :type json: dict
    """

    def __init__(
        self, language: str, cbor: Union[str, None] = None, json: Union[Dict, None] = None
    ):
        self.language = language
        self.cbor = cbor
        self.json = json
        if language == "native":
            self._schematype = om.Script1(language=self.language, cbor=self.cbor, json=self.json)
        else:
            self._schematype = om.Script2(language=self.language, cbor=self.cbor)


class Utxo:
    """A class representing a UTxO.

    :param tx_id: The transaction ID of the UTxO.
    :type tx_id: str
    :param index: The index of the UTxO.
    :type index: int
    :param address: The address of the UTxO.
    :type address: str
    :param value: The lovelace value of the UTxO (ada / 1e6)
    :type value: int
    :param datum_hash: The datum hash of the UTxO.
    :type datum_hash: str
    :param datum: The datum of the UTxO.
    :type datum: str
    :param script: The script of the UTxO.
    :type script: Script
    """

    def __init__(
        self,
        tx_id: str,
        index: int,
        address: str,
        value: dict,
        datum_hash: Union[str, None] = None,
        datum: Union[str, None] = None,
        script: Union[Script, None] = None,
    ):
        self.tx_id = tx_id
        self.index = index
        self.address = address
        self.value = value
        self.datum_hash = datum_hash
        self.datum = datum
        self.script = script

        # Not sure why the model Utxo class is a list of UtxoItems
        self._schematype = om.Utxo(
            __root__=[
                om.UtxoItem(
                    transaction=om.Transaction5(id=self.tx_id),
                    index=self.index,
                    address=om.Address(__root__=self.address),
                    value=om.Value(ada=om.Ada(lovelace=self.value.get("ada").get("lovelace"))),
                    datumHash=self.datum_hash,
                    datum=self.datum,
                    script=self.script,
                )
            ]
        )

    def __eq__(self, other):
        if isinstance(other, Utxo):
            if (
                self.tx_id == other.tx_id
                and self.index == other.index
                and self.address == other.address
                and self.value == other.value
                and self.datum_hash == other.datum_hash
                and self.datum == other.datum
                and self.script == other.script
            ):
                return True
        return False

    def __str__(self):
        return f"Utxo(TX={self.tx_id}, Index={self.index}, Address={self.address}, Value={self.value.get('ada'):,})"


class TxOutputReference:
    """A class representing a transaction output reference.

    :param transaction: The transaction ID of the output.
    :type transaction: str
    :param index: The index of the output.
    :type index: int
    """

    def __init__(self, tx_id: str, index: int):
        self.tx_id = tx_id
        self.index = index
        self._schematype = om.TransactionOutputReference(
            transaction={"id": self.tx_id}, index=self.index
        )


class Address:
    """A class representing an address."""

    def __init__(self, address: str):
        self.address = address
        self._schematype = om.Address(__root__=self.address)


class GenesisConfiguration:
    """A class representing the genesis configuration of the blockchain. Input parameters depend
    on the era of the genesis configuration.

    :raises InvalidOgmiosParameter: If an unsupported era is provided.
    """

    def __init__(
        self,
        era: Era,
        **kwargs,
    ):
        if era == Era.byron.value:
            self.era = era
            self.genesis_key_hashes = kwargs.get("genesis_key_hashes")
            self.genesis_delegations = kwargs.get("genesis_delegations")
            self.start_time = datetime.strptime(kwargs.get("start_time"), "%Y-%m-%dT%H:%M:%SZ")
            self.initial_funds = kwargs.get("initial_funds")
            self.initial_vouchers = kwargs.get("initial_vouchers")
            self.security_parameter = kwargs.get("security_parameter")
            self.network_magic = kwargs.get("network_magic")
            self.updatable_parameters = (
                BootstrapProtocolParameters(**kwargs.get("updatable_parameters"))
                if kwargs.get("updatable_parameters")
                else None
            )
            self._schematype = om.GenesisByron(
                era=self.era,
                genesisKeyHashes=self.genesis_key_hashes,
                genesisDelegations=self.genesis_delegations,
                startTime=self.start_time,
                initialFunds=self.initial_funds,
                initialVouchers=self.initial_vouchers,
                securityParameter=self.security_parameter,
                networkMagic=self.network_magic,
                updatableParameters=(
                    self.updatable_parameters._schema_type if self.updatable_parameters else None
                ),
            )
        elif era == Era.shelley.value:
            self.era = era
            self.start_time = datetime.strptime(kwargs.get("start_time"), "%Y-%m-%dT%H:%M:%SZ")
            self.network_magic = kwargs.get("network_magic")
            self.network = kwargs.get("network")
            self.active_slots_coefficient = float(Fraction(kwargs.get("active_slots_coefficient")))
            self.security_parameter = kwargs.get("security_parameter")
            self.epoch_length = kwargs.get("epoch_length")
            self.slots_per_kes_period = kwargs.get("slots_per_kes_period")
            self.max_kes_evolutions = kwargs.get("max_kes_evolutions")
            self.slot_length = kwargs.get("slot_length").get("milliseconds")
            self.update_quorum = kwargs.get("update_quorum")
            self.max_lovelace_supply = kwargs.get("max_lovelace_supply")
            self.initial_parameters = ProtocolParameters(**kwargs.get("initial_parameters"))
            self.initial_delegates = kwargs.get("initial_delegates")
            self.initial_funds = kwargs.get("initial_funds")
            self.initial_stake_pools = kwargs.get("initial_stake_pools")
            self._schematype = om.GenesisShelley(
                era=self.era,
                startTime=self.start_time,
                networkMagic=self.network_magic,
                network=self.network,
                activeSlotsCoefficient=kwargs.get("active_slots_coefficient"),
                securityParameter=self.security_parameter,
                epochLength=self.epoch_length,
                slotsPerKesPeriod=self.slots_per_kes_period,
                maxKesEvolutions=self.max_kes_evolutions,
                slotLength=kwargs.get("slot_length"),
                updateQuorum=self.update_quorum,
                maxLovelaceSupply=self.max_lovelace_supply,
                initialParameters=self.initial_parameters._schema_type,
                initialDelegates=self.initial_delegates,
                initialFunds=self.initial_funds,
                initialStakePools=self.initial_stake_pools,
            )
        elif era == Era.alonzo.value:
            self.era = era
            self.updatable_parameters = AlonzoUpdatableParameters(
                **kwargs.get("updatableParameters")
            )
            self._schematype = om.GenesisAlonzo(
                era=self.era, updatableParameters=self.updatable_parameters._schema_type
            )
        elif era == Era.conway.value:
            self.era = era
            self.constitution = kwargs.get("constitution")
            self.constitutional_committee = kwargs.get("constitutional_committee")
            self.updatable_parameters = ConwayUpdatableParameters(
                **kwargs.get("updatableParameters")
            )
            self._schematype = om.GenesisConway(
                era=self.era,
                constitution=self.constitution,
                constitutionalCommittee=self.constitutional_committee,
                updatableParameters=self.updatable_parameters._schema_type,
            )
        else:
            raise InvalidOgmiosParameter(f"Unsupported era for GenesisConfiguration: {era}")

    def __eq__(self, other):
        if isinstance(other, GenesisConfiguration):
            if (
                self.era == other.era
                and self.genesis_key_hashes == other.genesis_key_hashes
                and self.genesis_delegations == other.genesis_delegations
                and self.start_time == other.start_time
                and self.initial_funds == other.initial_funds
                and self.initial_vouchers == other.initial_vouchers
                and self.security_parameter == other.security_parameter
                and self.network_magic == other.network_magic
                and self.updatable_parameters == other.updatable_parameters
            ):
                return True
        return False


class EraSummary:
    """Summary of the slotting parameters and boundaries for each known era. Useful for doing
    slot-arithmetic and time conversions.

    :param start_time: The start time of the era (in seconds, relative to the network start).
    :type start_time: int
    :param start_slot: The start slot of the era.
    :type start_slot: int
    :param start_epoch: The start epoch of the era.
    :type start_epoch: int
    :param end_time: The end time of the era (in seconds, relative to the network start).
    :type end_time: int
    :param end_slot: The end slot of the era.
    :type end_slot: int
    :param end_epoch: The end epoch of the era.
    :type end_epoch: int
    :param epoch_length: The epoch length of the era.
    :type epoch_length: int
    :param slot_length: The slot length of the era, in milliseconds.
    :type slot_length: int
    :param safe_zone: Number of slots from the tip of the ledger in which it is guaranteed
        that no hard fork can take place. This should be (at least) the number of slots in
        which we are guaranteed to have k blocks.
    :type safe_zone: int
    """

    def __init__(
        self,
        start_time: int,
        start_slot: int,
        start_epoch: int,
        end_time: int,
        end_slot: int,
        end_epoch: int,
        epoch_length: int,
        slot_length: int,
        safe_zone: Union[int, None] = None,
    ):
        self.start_time = start_time
        self.start_slot = start_slot
        self.start_epoch = start_epoch
        self.end_time = end_time
        self.end_slot = end_slot
        self.end_epoch = end_epoch
        self.epoch_length = epoch_length
        self.slot_length = slot_length
        self.safe_zone = safe_zone
        self._schematype = None

    def __str__(self):
        return f"EraSummary(Time={self.start_time:,}-{self.end_time:,} s, Epoch={self.start_epoch:,}-{self.end_epoch:,}, Slot={self.start_slot:,}-{self.end_slot:,})"


class Ada:
    """A class representing an amount of ada.

    :param ada: The amount of ada.
    :type ada: int
    """

    def __init__(self, amount: int, is_lovelace: bool = False):
        self.ada = amount / 1e6 if is_lovelace else amount
        self.lovelace = int(amount if is_lovelace else amount * 1e6)
        self._schematype = om.Ada(lovelace=self.lovelace)

    def __eq__(self, other):
        if isinstance(other, Ada):
            if self.ada == other.ada:
                return True
        return False

    def __str__(self):
        return f"{self.ada:,} ADA"

    def __dict__(self):
        return {"ada": {"lovelace": self.lovelace}}


class BootstrapProtocolParameters:
    """A class representing the bootstrap protocol parameters of the blockchain.

    :param heavyDelegationThreshold: The heavy delegation threshold.
    :type heavyDelegationThreshold: dict
    :param maxBlockBodySize: The maximum block body size.
    :type maxBlockBodySize: dict
    :param maxBlockHeaderSize: The maximum block header size.
    :type maxBlockHeaderSize: dict
    :param maxUpdateProposalSize: The maximum update proposal size.
    :type maxUpdateProposalSize: dict
    :param maxTransactionSize: The maximum transaction size.
    :type maxTransactionSize: dict
    :param multiPartyComputationThreshold: The multi-party computation threshold.
    :type multiPartyComputationThreshold: dict
    :param scriptVersion: The script version.
    :type scriptVersion: int
    :param slotDuration: The slot duration.
    :type slotDuration: int
    :param unlockStakeEpoch: The unlock stake epoch.
    :type unlockStakeEpoch: int
    :param updateProposalThreshold: The update proposal threshold.
    :type updateProposalThreshold: dict
    :param updateProposalTimeToLive: The update proposal time to live.
    :type updateProposalTimeToLive: int
    :param updateVoteThreshold: The update vote threshold.
    :type updateVoteThreshold: dict
    :param softForkInitThreshold: The soft fork init threshold.
    :type softForkInitThreshold: dict
    :param softForkMinThreshold: The soft fork min threshold.
    :type softForkMinThreshold: dict
    :param softForkDecrementThreshold: The soft fork decrement threshold.
    :type softForkDecrementThreshold: dict
    :param minFeeCoefficient: The minimum fee coefficient.
    :type minFeeCoefficient: int
    :param minFeeConstant: The minimum fee constant.
    :type minFeeConstant: Lovelace
    """

    def __init__(
        self,
        heavyDelegationThreshold: dict = None,
        maxBlockBodySize: dict = None,
        maxBlockHeaderSize: dict = None,
        maxUpdateProposalSize: dict = None,
        maxTransactionSize: dict = None,
        multiPartyComputationThreshold: dict = None,
        scriptVersion: int = None,
        slotDuration: int = None,
        unlockStakeEpoch: int = None,
        updateProposalThreshold: dict = None,
        updateProposalTimeToLive: int = None,
        updateVoteThreshold: dict = None,
        softForkInitThreshold: dict = None,
        softForkMinThreshold: dict = None,
        softForkDecrementThreshold: dict = None,
        minFeeCoefficient: int = None,
        minFeeConstant: Ada = None,
    ):
        self.heavy_delegation_threshold = heavyDelegationThreshold
        self.max_block_body_size = maxBlockBodySize
        self.max_block_header_size = maxBlockHeaderSize
        self.max_update_proposal_size = maxUpdateProposalSize
        self.max_transaction_size = maxTransactionSize
        self.multi_party_computation_threshold = multiPartyComputationThreshold
        self.script_version = scriptVersion
        self.slot_duration = slotDuration
        self.unlock_stake_epoch = unlockStakeEpoch
        self.update_proposal_threshold = updateProposalThreshold
        self.update_proposal_time_to_live = updateProposalTimeToLive
        self.update_vote_threshold = updateVoteThreshold
        self.soft_fork_init_threshold = softForkInitThreshold
        self.soft_fork_min_threshold = softForkMinThreshold
        self.soft_fork_decrement_threshold = softForkDecrementThreshold
        self.min_fee_coefficient = minFeeCoefficient
        self.min_fee_constant = minFeeConstant
        self._schema_type = om.BootstrapProtocolParameters(
            heavyDelegationThreshold=self.heavy_delegation_threshold,
            maxBlockBodySize=self.max_block_body_size,
            maxBlockHeaderSize=self.max_block_header_size,
            maxUpdateProposalSize=self.max_update_proposal_size,
            maxTransactionSize=self.max_transaction_size,
            multiPartyComputationThreshold=self.multi_party_computation_threshold,
            scriptVersion=self.script_version,
            slotDuration=self.slot_duration,
            unlockStakeEpoch=self.unlock_stake_epoch,
            updateProposalThreshold=self.update_proposal_threshold,
            updateProposalTimeToLive=self.update_proposal_time_to_live,
            updateVoteThreshold=self.update_vote_threshold,
            softForkInitThreshold=self.soft_fork_init_threshold,
            softForkMinThreshold=self.soft_fork_min_threshold,
            softForkDecrementThreshold=self.soft_fork_decrement_threshold,
            minFeeCoefficient=self.min_fee_coefficient,
            minFeeConstant=self.min_fee_constant,
        )

    def _to_ada(self, value: Union[int, Dict, Ada]) -> Ada:
        if isinstance(value, Ada):
            return value
        elif isinstance(value, dict):
            return Ada(value.get("ada").get("lovelace"), is_lovelace=True)
        elif isinstance(value, int):
            return Ada(value)
        else:
            raise InvalidOgmiosParameter(f"Invalid type for value {value}: {type(value)}")


class ProtocolParameters:
    """A class representing the protocol parameters of the blockchain.

    :param minFeeCoefficient: The minimum fee coefficient.
    :type minFeeCoefficient: int
    :param minFeeConstant: The minimum fee constant.
    :type minFeeConstant: Ada
    :param minUtxoDepositCoefficient: The minimum UTXO deposit coefficient.
    :type minUtxoDepositCoefficient: int
    :param minUtxoDepositConstant: The minimum UTXO deposit constant.
    :type minUtxoDepositConstant: Ada
    :param maxBlockBodySize: The maximum block body size.
    :type maxBlockBodySize: dict
    :param maxBlockHeaderSize: The maximum block header size.
    :type maxBlockHeaderSize: dict
    :param stakeCredentialDeposit: The stake credential deposit.
    :type stakeCredentialDeposit: Ada
    :param stakePoolDeposit: The stake pool deposit.
    :type stakePoolDeposit: Ada
    :param stakePoolRetirementEpochBound: The stake pool retirement epoch bound.
    :type stakePoolRetirementEpochBound: int
    :param stakePoolPledgeInfluence: The stake pool pledge influence.
    :type stakePoolPledgeInfluence: str
    :param minStakePoolCost: The minimum stake pool cost.
    :type minStakePoolCost: Ada
    :param desiredNumberOfStakePools: The desired number of stake pools.
    :type desiredNumberOfStakePools: int
    :param monetaryExpansion: The monetary expansion.
    :type monetaryExpansion: str
    :param treasuryExpansion: The treasury expansion.
    :type treasuryExpansion: str
    :param version: The version.
    :type version: dict
    :param collateralPercentage: The collateral percentage.
    :type collateralPercentage: int
    :param maxCollateralInputs: The maximum collateral inputs.
    :type maxCollateralInputs: int
    :param plutusCostModels: The plutus cost models.
    :type plutusCostModels: dict
    :param scriptExecutionPrices: The script execution prices.
    :type scriptExecutionPrices: dict
    :param maxExecutionUnitsPerTransaction: The maximum execution units per transaction.
    :type maxExecutionUnitsPerTransaction: dict
    :param maxExecutionUnitsPerBlock: The maximum execution units per block.
    :type maxExecutionUnitsPerBlock: dict
    :param maxValueSize: The maximum value size.
    :type maxValueSize: dict
    :param extraEntropy: The extra entropy.
    :type extraEntropy: str
    :param maxTransactionSize: The maximum transaction size.
    :type maxTransactionSize: dict
    :param federatedBlockProductionRatio: The federated block production ratio.
    :type federatedBlockProductionRatio: str
    :param maximumReferenceScriptsSize: The maximum reference scripts size.
    :type maximumReferenceScriptsSize: dict
    :param minFeeReferenceScripts: The minimum fee reference scripts.
    :type minFeeReferenceScripts: dict
    :param stakePoolVotingThresholds: The stake pool voting thresholds.
    :type stakePoolVotingThresholds: dict
    :param delegateRepresentativeVotingThresholds: The delegate representative voting thresholds.
    :type delegateRepresentativeVotingThresholds: dict
    :param constitutionalCommitteeMinSize: The constitutional committee minimum size.
    :type constitutionalCommitteeMinSize: int
    :param constitutionalCommitteeMaxTermLength: The constitutional committee maximum term length.
    :type constitutionalCommitteeMaxTermLength: int
    :param governanceActionLifetime: The governance action lifetime.
    :type governanceActionLifetime: int
    :param governanceActionDeposit: The governance action deposit.
    :type governanceActionDeposit: Ada
    :param delegateRepresentativeDeposit: The delegate representative deposit.
    :type delegateRepresentativeDeposit: Ada
    :param delegateRepresentativeMaxIdleTime: The delegate representative maximum idle time.
    :type delegateRepresentativeMaxIdleTime: int
    """

    def __init__(
        self,
        minFeeCoefficient: int,
        minFeeConstant: Union[int, Ada],
        minUtxoDepositCoefficient: int,
        minUtxoDepositConstant: Union[int, Ada],
        maxBlockBodySize: dict,
        maxBlockHeaderSize: dict,
        stakeCredentialDeposit: Ada,
        stakePoolDeposit: Union[int, Ada],
        stakePoolRetirementEpochBound: int,
        stakePoolPledgeInfluence: str,
        minStakePoolCost: Union[int, Ada],
        desiredNumberOfStakePools: int,
        monetaryExpansion: str,
        treasuryExpansion: str,
        version: dict,
        collateralPercentage: int = None,
        maxCollateralInputs: int = None,
        plutusCostModels: dict = None,
        scriptExecutionPrices: dict = None,
        maxExecutionUnitsPerTransaction: dict = None,
        maxExecutionUnitsPerBlock: dict = None,
        maxValueSize: dict = None,
        extraEntropy: str = None,
        maxTransactionSize: dict = None,
        federatedBlockProductionRatio: str = None,
        maxReferenceScriptsSize: dict = None,
        minFeeReferenceScripts: dict = None,
        stakePoolVotingThresholds: dict = None,
        delegateRepresentativeVotingThresholds: dict = None,
        constitutionalCommitteeMinSize: int = None,
        constitutionalCommitteeMaxTermLength: int = None,
        governanceActionLifetime: int = None,
        governanceActionDeposit: Ada = None,
        delegateRepresentativeDeposit: Ada = None,
        delegateRepresentativeMaxIdleTime: int = None,
    ):
        self.min_fee_coefficient = minFeeCoefficient
        self.min_fee_constant = self._to_ada(minFeeConstant)
        self.min_utxo_deposit_coefficient = minUtxoDepositCoefficient
        self.min_utxo_deposit_constant = self._to_ada(minUtxoDepositConstant)
        self.max_block_body_size = maxBlockBodySize
        self.max_block_header_size = maxBlockHeaderSize
        self.max_transaction_size = maxTransactionSize
        self.max_value_size = maxValueSize
        self.extra_entropy = extraEntropy
        self.stake_credential_deposit = self._to_ada(stakeCredentialDeposit)
        self.stake_pool_deposit = self._to_ada(stakePoolDeposit)
        self.stake_pool_retirement_epoch_bound = stakePoolRetirementEpochBound
        self.stake_pool_pledge_influence = stakePoolPledgeInfluence
        self.min_stake_pool_cost = self._to_ada(minStakePoolCost)
        self.desired_number_of_stake_pools = desiredNumberOfStakePools
        self.federated_block_production_ratio = federatedBlockProductionRatio
        self.monetary_expansion = monetaryExpansion
        self.treasury_expansion = treasuryExpansion
        self.collateral_percentage = collateralPercentage
        self.max_collateral_inputs = maxCollateralInputs
        self.plutus_cost_models = plutusCostModels
        self.script_execution_prices = scriptExecutionPrices
        self.max_execution_units_per_transaction = maxExecutionUnitsPerTransaction
        self.max_execution_units_per_block = maxExecutionUnitsPerBlock
        self.max_ref_script_size = maxReferenceScriptsSize
        self.min_fee_ref_scripts = minFeeReferenceScripts
        self.stake_pool_voting_thresholds = stakePoolVotingThresholds
        self.delegate_representative_voting_thresholds = delegateRepresentativeVotingThresholds
        self.constitutional_committee_min_size = constitutionalCommitteeMinSize
        self.constitutional_committee_max_term_length = constitutionalCommitteeMaxTermLength
        self.governance_action_lifetime = governanceActionLifetime
        self.governance_action_deposit = self._to_ada(governanceActionDeposit)
        self.delegate_representative_deposit = self._to_ada(delegateRepresentativeDeposit)
        self.delegate_representative_max_idle_time = delegateRepresentativeMaxIdleTime
        self.version = version
        self._schema_type = om.ProtocolParameters(
            minFeeCoefficient=self.min_fee_coefficient,
            minFeeConstant=self.min_fee_constant.__dict__(),
            minUtxoDepositCoefficient=self.min_utxo_deposit_coefficient,
            minUtxoDepositConstant=self.min_utxo_deposit_constant.__dict__(),
            maxBlockBodySize=self.max_block_body_size,
            maxBlockHeaderSize=self.max_block_header_size,
            maxTransactionSize=self.max_transaction_size,
            maxValueSize=self.max_value_size,
            extraEntropy=self.extra_entropy,
            stakeCredentialDeposit=self.stake_credential_deposit.__dict__(),
            stakePoolDeposit=self.stake_pool_deposit.__dict__(),
            stakePoolRetirementEpochBound=self.stake_pool_retirement_epoch_bound,
            stakePoolPledgeInfluence=self.stake_pool_pledge_influence,
            minStakePoolCost=self.min_stake_pool_cost.__dict__(),
            desiredNumberOfStakePools=self.desired_number_of_stake_pools,
            federatedBlockProductionRatio=self.federated_block_production_ratio,
            monetaryExpansion=self.monetary_expansion,
            treasuryExpansion=self.treasury_expansion,
            collateralPercentage=self.collateral_percentage,
            maxCollateralInputs=self.max_collateral_inputs,
            plutusCostModels=self.plutus_cost_models,
            scriptExecutionPrices=self.script_execution_prices,
            maxExecutionUnitsPerTransaction=self.max_execution_units_per_transaction,
            maxExecutionUnitsPerBlock=self.max_execution_units_per_block,
            version=self.version,
            minFeeReferenceScripts=self.min_fee_ref_scripts,
            maxReferenceScriptsSize=self.max_ref_script_size,
            stakePoolVotingThresholds=self.stake_pool_voting_thresholds,
            delegateRepresentativeVotingThresholds=self.delegate_representative_voting_thresholds,
            constitutionalCommitteeMinSize=self.constitutional_committee_min_size,
            constitutionalCommitteeMaxTermLength=self.constitutional_committee_max_term_length,
            governanceActionLifetime=self.governance_action_lifetime,
            governanceActionDeposit=(
                self.governance_action_deposit.__dict__()
                if self.governance_action_deposit
                else None
            ),
            delegateRepresentativeDeposit=(
                self.delegate_representative_deposit.__dict__()
                if self.delegate_representative_deposit
                else None
            ),
            delegateRepresentativeMaxIdleTime=self.delegate_representative_max_idle_time,
        )

    def _to_ada(self, value: Union[int, dict, Ada]) -> Ada:
        if value is None:
            return None
        elif isinstance(value, Ada):
            return value
        elif isinstance(value, dict):
            return Ada(value.get("ada").get("lovelace"), is_lovelace=True)
        # elif isinstance(value, int):
        #     return Ada(value)
        else:
            raise InvalidOgmiosParameter(f"Invalid type for value {value}: {type(value)}")


class AlonzoUpdatableParameters:
    """A class representing the Alonzo updatable parameters of the blockchain.

    :param minUtxoDepositCoefficient: The minimum UTXO deposit coefficient.
    :type minUtxoDepositCoefficient: int
    :param collateralPercentage: The collateral percentage.
    :type collateralPercentage: int
    :param plutusCostModels: The plutus cost models.
    :type plutusCostModels: dict
    :param maxCollateralInputs: The maximum collateral inputs.
    :type maxCollateralInputs: int
    :param maxExecutionUnitsPerBlock: The maximum execution units per block.
    :type maxExecutionUnitsPerBlock: dict
    :param maxExecutionUnitsPerTransaction: The maximum execution units per transaction.
    :type maxExecutionUnitsPerTransaction: dict
    :param maxValueSize: The maximum value size.
    :type maxValueSize: dict
    :param scriptExecutionPrices: The script execution prices.
    :type scriptExecutionPrices: dict
    """

    def __init__(
        self,
        minUtxoDepositCoefficient: int,
        collateralPercentage: int,
        plutusCostModels: dict,
        maxCollateralInputs: int,
        maxExecutionUnitsPerBlock: dict,
        maxExecutionUnitsPerTransaction: dict,
        maxValueSize: dict,
        scriptExecutionPrices: dict,
    ):
        self.min_utxo_deposit_coefficient = minUtxoDepositCoefficient
        self.collateral_percentage = collateralPercentage
        self.plutus_cost_models = plutusCostModels
        self.max_collateral_inputs = maxCollateralInputs
        self.max_execution_units_per_block = maxExecutionUnitsPerBlock
        self.max_execution_units_per_transaction = maxExecutionUnitsPerTransaction
        self.max_value_size = maxValueSize
        self.script_execution_prices = scriptExecutionPrices
        self._schema_type = om.UpdatableParameters(
            minUtxoDepositCoefficient=self.min_utxo_deposit_coefficient,
            collateralPercentage=self.collateral_percentage,
            plutusCostModels=self.plutus_cost_models,
            maxCollateralInputs=self.max_collateral_inputs,
            maxExecutionUnitsPerBlock=self.max_execution_units_per_block,
            maxExecutionUnitsPerTransaction=self.max_execution_units_per_transaction,
            maxValueSize=self.max_value_size,
            scriptExecutionPrices=self.script_execution_prices,
        )


class ConwayUpdatableParameters:
    """A class representing the Conway updatable parameters of the blockchain.

    :param stakePoolVotingThresholds: The stake pool voting thresholds.
    :type stakePoolVotingThresholds: dict
    :param delegateRepresentativeVotingThresholds: The delegate representative voting thresholds.
    :type delegateRepresentativeVotingThresholds: dict
    :param constitutionalCommitteeMinSize: The constitutional committee minimum size.
    :type constitutionalCommitteeMinSize: int
    :param constitutionalCommitteeMaxTermLength: The constitutional committee maximum term length.
    :type constitutionalCommitteeMaxTermLength: int
    :param governanceActionLifetime: The governance action lifetime.
    :type governanceActionLifetime: int
    :param governanceActionDeposit: The governance action deposit.
    :type governanceActionDeposit: Ada
    :param delegateRepresentativeDeposit: The delegate representative deposit.
    :type delegateRepresentativeDeposit: Ada
    :param delegateRepresentativeMaxIdleTime: The delegate representative maximum idle time.
    :type delegateRepresentativeMaxIdleTime: int
    """

    def __init__(
        self,
        stakePoolVotingThresholds: dict,
        delegateRepresentativeVotingThresholds: dict,
        constitutionalCommitteeMinSize: int,
        constitutionalCommitteeMaxTermLength: int,
        governanceActionLifetime: int,
        governanceActionDeposit: Ada,
        delegateRepresentativeDeposit: Ada,
        delegateRepresentativeMaxIdleTime: int,
    ):
        self.stake_pool_voting_thresholds = stakePoolVotingThresholds
        self.delegate_representative_voting_thresholds = delegateRepresentativeVotingThresholds
        self.constitutional_committee_min_size = constitutionalCommitteeMinSize
        self.constitutional_committee_max_term_length = constitutionalCommitteeMaxTermLength
        self.governance_action_lifetime = governanceActionLifetime
        self.governance_action_deposit = governanceActionDeposit
        self.delegate_representative_deposit = delegateRepresentativeDeposit
        self.delegate_representative_max_idle_time = delegateRepresentativeMaxIdleTime
        self._schema_type = om.UpdatableParameters1(
            stakePoolVotingThresholds=self.stake_pool_voting_thresholds,
            delegateRepresentativeVotingThresholds=self.delegate_representative_voting_thresholds,
            constitutionalCommitteeMinSize=self.constitutional_committee_min_size,
            constitutionalCommitteeMaxTermLength=self.constitutional_committee_max_term_length,
            governanceActionLifetime=self.governance_action_lifetime,
            governanceActionDeposit=self.governance_action_deposit,
            delegateRepresentativeDeposit=self.delegate_representative_deposit,
            delegateRepresentativeMaxIdleTime=self.delegate_representative_max_idle_time,
        )


class GovernanceProtocolParameters:
    """A class representing the governance protocol parameters of the blockchain.

    NOTE: This data structure is currently missing from the ogmios schema, so strict
    type checking is not enforced. This will be updated when the schema is updated.

    :param stakePoolVotingThresholds: The stake pool voting thresholds.
    :type stakePoolVotingThresholds: dict
    :param delegateRepresentativeVotingThresholds: The delegate representative voting thresholds.
    :type delegateRepresentativeVotingThresholds: dict
    :param constitutionalCommitteeMinSize: The constitutional committee minimum size.
    :type constitutionalCommitteeMinSize: int
    :param constitutionalCommitteeMaxTermLength: The constitutional committee maximum term length.
    :type constitutionalCommitteeMaxTermLength: int
    :param governanceActionLifetime: The governance action lifetime.
    :type governanceActionLifetime: int
    :param governanceActionDeposit: The governance action deposit.
    :type governanceActionDeposit: Ada
    :param delegateRepresentativeDeposit: The delegate representative deposit.
    :type delegateRepresentativeDeposit: Ada
    :param delegateRepresentativeMaxIdleTime: The delegate representative maximum idle time.
    :type delegateRepresentativeMaxIdleTime: int
    """

    def __init__(
        self,
        stakePoolVotingThresholds: dict,
        delegateRepresentativeVotingThresholds: dict,
        constitutionalCommitteeMinSize: int,
        constitutionalCommitteeMaxTermLength: int,
        governanceActionLifetime: int,
        governanceActionDeposit: Ada,
        delegateRepresentativeDeposit: Ada,
        delegateRepresentativeMaxIdleTime: int,
    ):
        self.stake_pool_voting_thresholds = stakePoolVotingThresholds
        self.delegate_representative_voting_thresholds = delegateRepresentativeVotingThresholds
        self.constitutional_committee_min_size = constitutionalCommitteeMinSize
        self.constitutional_committee_max_term_length = constitutionalCommitteeMaxTermLength
        self.governance_action_lifetime = governanceActionLifetime
        self.governance_action_deposit = governanceActionDeposit
        self.delegate_representative_deposit = delegateRepresentativeDeposit
        self.delegate_representative_max_idle_time = delegateRepresentativeMaxIdleTime
        self._schema_type = None
