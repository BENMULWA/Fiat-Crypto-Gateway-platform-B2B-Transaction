"""This file contains convenient mappings from ogmios.datatypes to ogmios.model.ogmios_model.
    These mappings combine logical types from the ogmios model that are separated in the schema into
    combined enums that are easier to work with.
"""

from enum import Enum

import ogmios.model.ogmios_model as om


class Types(Enum):
    ebb = om.Type.ebb.value
    bft = om.Type1.bft.value
    praos = om.Type2.praos.value
    stakeDelegation = om.Type3.stakeDelegation.value
    stakeCredentialRegistration = om.Type4.stakeCredentialRegistration.value
    stakeCredentialDeregistration = om.Type5.stakeCredentialDeregistration.value
    stakePoolRegistration = om.Type6.stakePoolRegistration.value
    stakePoolRetirement = om.Type7.stakePoolRetirement.value
    genesisDelegation = om.Type8.genesisDelegation.value
    constitutionalCommitteeDelegation = om.Type9.constitutionalCommitteeDelegation.value
    constitutionalCommitteeRetirement = om.Type10.constitutionalCommitteeRetirement.value
    delegateRepresentativeRegistration = om.Type11.delegateRepresentativeRegistration.value
    delegateRepresentativeUpdate = om.Type12.delegateRepresentativeUpdate.value
    delegateRepresentativeRetirement = om.Type13.delegateRepresentativeRetirement.value
    registered = om.Type14.registered.value
    noConfidence = om.Type15.noConfidence.value
    abstain = om.Type16.abstain.value
    protocolParametersUpdate = om.Type17.protocolParametersUpdate.value
    hardForkInitiation = om.Type18.hardForkInitiation.value
    treasuryTransfer = om.Type19.treasuryTransfer.value
    treasuryWithdrawals = om.Type20.treasuryWithdrawals.value
    constitutionalCommittee = om.Type21.constitutionalCommittee.value
    constitution = om.Type22.constitution.value
    information = om.Type24.information.value
    ipAddress = om.Type25.ipAddress.value
    hostname = om.Type26.hostname.value


class Method(Enum):
    findIntersection = om.Method.findIntersection.value
    nextBlock = om.Method4.nextBlock.value
    submitTransaction = om.Method6.submitTransaction.value
    evaluateTransaction = om.Method10.evaluateTransaction.value
    acquireLedgerState = om.Method14.acquireLedgerState.value
    releaseLedgerState = om.Method17.releaseLedgerState.value
    queryLedgerState_epoch = om.Method19.queryLedgerState_epoch.value
    queryLedgerState_eraStart = om.Method19.queryLedgerState_eraStart.value
    queryLedgerState_eraSummaries = om.Method19.queryLedgerState_eraSummaries.value
    queryLedgerState_liveStakeDistribution = (
        om.Method19.queryLedgerState_liveStakeDistribution.value
    )
    queryLedgerState_projectedRewards = om.Method19.queryLedgerState_projectedRewards.value
    queryLedgerState_protocolParameters = om.Method19.queryLedgerState_protocolParameters.value
    queryLedgerState_proposedProtocolParameters = (
        om.Method19.queryLedgerState_proposedProtocolParameters
    ).value
    queryLedgerState_rewardAccountSummaries = (
        om.Method19.queryLedgerState_rewardAccountSummaries.value
    )
    queryLedgerState_rewardsProvenance = om.Method19.queryLedgerState_rewardsProvenance.value
    queryLedgerState_stakePools = om.Method19.queryLedgerState_stakePools.value
    queryLedgerState_utxo = om.Method19.queryLedgerState_utxo.value
    queryLedgerState_tip = om.Method19.queryLedgerState_tip.value
    queryLedgerState_constitution = om.Method19.queryLedgerState_constitution.value
    queryLedgerState_constitutionalCommittee = (
        om.Method19.queryLedgerState_constitutionalCommittee.value
    )
    queryLedgerState_treasuryAndReserves = om.Method19.queryLedgerState_treasuryAndReserves.value
    queryNetwork_blockHeight = om.Method52.queryNetwork_blockHeight.value
    queryNetwork_genesisConfiguration = om.Method54.queryNetwork_genesisConfiguration.value
    queryNetwork_startTime = om.Method57.queryNetwork_startTime.value
    queryNetwork_tip = om.Method59.queryNetwork_tip.value
    acquireMempool = om.Method61.acquireMempool.value
    nextTransaction = om.Method63.nextTransaction.value
    hasTransaction = om.Method64.hasTransaction.value
    sizeOfMempool = om.Method64.sizeOfMempool.value
    releaseMempool = om.Method64.releaseMempool.value
