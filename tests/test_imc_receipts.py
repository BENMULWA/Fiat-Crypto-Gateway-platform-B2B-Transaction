"""IMC receipts (services/imc_receipts.py) and the manual-mint wait that claims them.

The chain and the database are in-memory doubles: nothing touches Celo or Mongo."""
import asyncio
from datetime import datetime

import pytest

import services.imc_receipts as rc
from Brain_Engine.state_engine import FSMState, HFTCorridorFSM, ImmutableLedger

TOKEN = "0x766AA4F469A295330b10D150f842d71977D56dD6"
TREASURY = "0x6f7BeAb48E0000000000000000000000000000AA"
OTHER = "0x1111111111111111111111111111111111111111"
MINTER = "0x89f6Bb5B25A2c8c13a7dc0266380468BFEb991d6"
T0 = datetime(2026, 10, 7, 2, 0, 0)              # a run started at 02:00 UTC


def ts(dt: datetime) -> int:
    return int((dt - datetime(1970, 1, 1)).total_seconds())


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ doubles
def _match(doc, query):
    for k, cond in query.items():
        if doc.get(k) != cond:
            return False
    return True


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, key, direction):
        self.docs = sorted(self.docs, key=lambda d: d.get(key), reverse=direction < 0)
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self.docs)


class Col:
    def __init__(self):
        self.docs = {}

    async def insert_one(self, d):
        if d["_id"] in self.docs:
            raise RuntimeError("duplicate key")
        self.docs[d["_id"]] = dict(d)

    async def find_one(self, q):
        return next((dict(d) for d in self.docs.values() if _match(d, q)), None)

    def find(self, q=None):
        return Cursor([dict(d) for d in self.docs.values() if _match(d, q or {})])

    async def delete_one(self, q):
        for k, d in list(self.docs.items()):
            if _match(d, q):
                del self.docs[k]
                return {"deleted_count": 1}
        return {"deleted_count": 0}

    async def update_one(self, q, u):
        for d in self.docs.values():
            if _match(d, q):
                d.update(u.get("$set", {}))
                return {"matched_count": 1, "modified_count": 1}
        return {"matched_count": 0, "modified_count": 0}


class DB:
    def __init__(self):
        self.c = {}

    def __getitem__(self, n):
        return self.c.setdefault(n, Col())


def mk_log(tx, idx, block, to, amount_imc, frm=MINTER, token=TOKEN):
    return {
        "address": token, "blockNumber": block, "logIndex": idx,
        "transactionHash": tx.removeprefix("0x"),                      # hexbytes >= 1.0 gives no 0x prefix
        "topics": [rc.TRANSFER_TOPIC, rc.pad_topic(frm), rc.pad_topic(to)],
        "data": hex(int(round(amount_imc * 1e6)))[2:].rjust(64, "0"),
    }


class Eth:
    def __init__(self):
        self.block_number = 1000
        self.logs = []
        self.block_ts = {}
        self.receipts = {}
        self.log_calls = []

    def get_logs(self, p):
        self.log_calls.append((p["fromBlock"], p["toBlock"]))
        want_to = p["topics"][2]
        return [l for l in self.logs if p["fromBlock"] <= l["blockNumber"] <= p["toBlock"]
                and l["address"].lower() == p["address"].lower() and l["topics"][2] == want_to]

    def get_block(self, n):
        return {"timestamp": self.block_ts.get(n, ts(T0))}

    def get_transaction_receipt(self, h):
        if h not in self.receipts:
            raise KeyError("not found")
        return self.receipts[h]


class W3:
    def __init__(self):
        self.eth = Eth()


def tx(n):
    return "0x" + f"{n:x}".rjust(64, "0")


def make(w3=None, db=None):
    w3 = w3 or W3()
    db = db or DB()
    return rc.ImcReceipts(db, w3=w3, token=TOKEN, treasury=TREASURY), w3, db


def land(w3, n, amount, block, when: datetime, to=TREASURY, status=1):
    """Put a mint on the fake chain: a log for scans and a receipt for attach."""
    log = mk_log(tx(n), 0, block, to, amount)
    w3.eth.logs.append(log)
    w3.eth.block_ts[block] = ts(when)
    w3.eth.receipts[tx(n)] = {"status": status, "logs": [log]}
    return tx(n)


# ------------------------------------------------------------------ helpers
def test_hex_helpers_handle_hexbytes_without_prefix():
    class HB(bytes):
        def hex(self):                     # hexbytes >= 1.0: no 0x
            return bytes(self).hex()

    assert rc.hex0x(HB(b"\xab\xcd")) == "0xabcd"
    assert rc.hex0x("ABCD") == "0xabcd" and rc.hex0x("0xABCD") == "0xabcd"


def test_parse_transfer_reads_amount_and_addresses():
    t = rc.parse_transfer(mk_log(tx(1), 3, 77, TREASURY, 0.363512))
    assert t["amountImc"] == pytest.approx(0.363512) and t["amountBase"] == 363512
    assert t["to"].lower() == TREASURY.lower() and t["from"].lower() == MINTER.lower()
    assert t["txHash"] == tx(1) and t["logIndex"] == 3 and t["blockNumber"] == 77


def test_within_is_a_one_percent_band_not_at_least():
    assert rc.within(0.0811, 0.0811) and rc.within(0.0815, 0.0811) and rc.within(0.0808, 0.0811)
    assert not rc.within(0.363512, 0.0811)      # the real mint from the screenshot is not this run's
    assert not rc.within(0.05, 0.0811) and not rc.within(0.1, 0)


@pytest.mark.parametrize("bad", ["", "0x123", "not a hash", "0x" + "z" * 64])
def test_normalize_rejects_non_hashes(bad):
    with pytest.raises(rc.AttachError):
        rc.normalize_tx_hash(bad)


def test_normalize_adds_the_prefix_and_lowercases():
    assert rc.normalize_tx_hash("AB" * 32) == "0x" + "ab" * 32


# ------------------------------------------------------------------ scanning
def test_scan_records_each_incoming_transfer_once():
    svc, w3, db = make()
    land(w3, 1, 0.363512, 990, T0)
    assert run(svc.scan()) == 1
    assert run(svc.scan()) == 0                                  # nothing new, nothing duplicated
    (only,) = db[rc.COLLECTION].docs.values()
    assert only["amountImc"] == pytest.approx(0.363512) and only["claimedByRun"] is None
    assert only["blockTime"] == T0


def test_scan_ignores_other_recipients_and_other_tokens():
    svc, w3, db = make()
    land(w3, 1, 5.0, 990, T0, to=OTHER)
    w3.eth.logs.append(mk_log(tx(2), 0, 991, TREASURY, 9.0, token=OTHER))
    assert run(svc.scan()) == 0 and db[rc.COLLECTION].docs == {}


def test_scan_queries_in_chunks_and_continues_from_the_cursor(monkeypatch):
    monkeypatch.setattr(rc, "CHUNK_BLOCKS", 100)
    svc, w3, db = make()
    w3.eth.block_number = 1000
    land(w3, 1, 1.0, 700, T0)
    assert run(svc.scan(lookback_blocks=450)) == 1               # blocks 550..1000 are queried
    assert all(b - a + 1 <= 100 for a, b in w3.eth.log_calls)
    assert w3.eth.log_calls[0][0] == 550 and w3.eth.log_calls[-1][1] == 1000
    w3.eth.log_calls.clear()
    w3.eth.block_number = 1050
    land(w3, 2, 2.0, 1030, T0)
    assert run(svc.scan()) == 1
    assert w3.eth.log_calls == [(1001, 1050)]                    # picks up exactly where it stopped


# ------------------------------------------------------------------ claiming
def test_a_run_claims_only_a_receipt_within_one_percent():
    svc, w3, db = make()
    land(w3, 1, 0.363512, 990, T0)                              # the screenshot's mint
    run(svc.scan())
    assert run(svc.claim_match("RUN-A", 0.081081, since=None)) is None   # not this run's amount
    w3.eth.block_number = 1010                                   # time passes: new blocks are produced
    land(w3, 2, 0.0811, 1005, T0)
    run(svc.scan())
    got = run(svc.claim_match("RUN-A", 0.081081, since=None))
    assert got["txHash"] == tx(2) and got["claimedByRun"] == "RUN-A"


def test_a_receipt_is_spent_once():
    svc, w3, db = make()
    land(w3, 1, 0.0811, 990, T0)
    run(svc.scan())
    assert run(svc.claim_match("RUN-A", 0.0811, None)) is not None
    assert run(svc.claim_match("RUN-B", 0.0811, None)) is None
    assert run(svc.claim_match("RUN-A", 0.0811, None)) is None   # and not twice by the same run either


def test_a_receipt_from_before_the_run_started_is_not_auto_claimed():
    svc, w3, db = make()
    land(w3, 1, 0.0811, 990, datetime(2026, 10, 6, 22, 0, 0))   # minted the evening before
    run(svc.scan())
    assert run(svc.claim_match("RUN-A", 0.0811, since=T0)) is None


def test_the_oldest_matching_receipt_is_claimed_first():
    svc, w3, db = make()
    land(w3, 1, 0.0811, 990, datetime(2026, 10, 7, 3, 0, 0))
    land(w3, 2, 0.0811, 991, datetime(2026, 10, 7, 2, 30, 0))
    run(svc.scan())
    assert run(svc.claim_match("RUN-A", 0.0811, T0))["txHash"] == tx(2)


# ------------------------------------------------------------------ attach by hash
def test_attach_reserves_a_verified_transfer_even_if_it_predates_the_run():
    svc, w3, db = make()
    h = land(w3, 7, 0.0811, 990, datetime(2026, 10, 6, 22, 0, 0))
    rec = run(svc.attach("RUN-A", h, 0.081081))
    assert rec["claimedByRun"] == "RUN-A"
    assert run(svc.find_for_run("RUN-A", 0.081081, since=T0))["txHash"] == h      # reserved, so the run finds it
    assert run(svc.attach("RUN-A", h, 0.081081))["claimedByRun"] == "RUN-A"        # attaching again is harmless


def test_attach_rejects_the_wrong_amount_and_says_both_numbers():
    svc, w3, db = make()
    h = land(w3, 1, 0.363512, 990, T0)
    with pytest.raises(rc.AttachError) as e:
        run(svc.attach("RUN-A", h, 0.081081))
    assert "0.363512" in str(e.value) and "0.081081" in str(e.value)
    assert db[rc.COLLECTION].docs[f"{h}:0"]["claimedByRun"] is None               # recorded, but not claimed


def test_attach_rejects_other_recipients_failed_and_unknown_transactions():
    svc, w3, db = make()
    elsewhere = land(w3, 1, 0.0811, 990, T0, to=OTHER)
    with pytest.raises(rc.AttachError, match="did not send IMC to the treasury"):
        run(svc.attach("RUN-A", elsewhere, 0.0811))
    failed = land(w3, 2, 0.0811, 991, T0, status=0)
    with pytest.raises(rc.AttachError, match="failed on-chain"):
        run(svc.attach("RUN-A", failed, 0.0811))
    with pytest.raises(rc.AttachError, match="not found"):
        run(svc.attach("RUN-A", tx(99), 0.0811))


def test_attach_refuses_imc_already_used_by_another_run():
    svc, w3, db = make()
    h = land(w3, 1, 0.0811, 990, T0)
    run(svc.attach("RUN-A", h, 0.0811))
    with pytest.raises(rc.AttachError, match="already used by run RUN-A"):
        run(svc.attach("RUN-B", h, 0.0811))


def test_record_tx_stores_a_transfer_without_claiming_it():
    svc, w3, db = make()
    h = land(w3, 1, 0.363512, 990, T0)
    (rec,) = run(svc.record_tx(h))
    assert rec["amountImc"] == pytest.approx(0.363512) and rec["claimedByRun"] is None and rec["source"] == "manual"


# ------------------------------------------------------------------ the run's manual-mint wait
def fsm(svc, expected=0.081081, started=T0):
    async def swap_rate(a, b):
        return 1.0

    ledger = ImmutableLedger(db_collection=None)
    bot = HFTCorridorFSM(ledger, starting_capital_usd=0.0772, config={
        "node_procure": "N2", "node_liquidate": "N5", "discount": 0.05, "baseline_rate": 129.50,
        "run_id": "RUN-A", "imc_receipts": svc, "run_started_at": started, "poll_seconds": 0.001,
        "get_swap_rate_fn": swap_rate, "get_treasury_usdt_balance_fn": lambda: 100.0,
    })
    bot.state = FSMState.AWAITING_MANUAL_MINT
    bot.pending_mint_expected_imc = expected
    bot.pending_mint_external_id = "MINT-RUN-A-C1"
    return bot, ledger


def test_the_run_books_a_mint_that_lands_while_it_waits():
    svc, w3, db = make()
    bot, ledger = fsm(svc)
    run(bot._check_manual_mint())
    assert bot.state == FSMState.AWAITING_MANUAL_MINT                       # nothing yet: keeps waiting

    w3.eth.block_number = 1010                                               # new blocks are produced
    land(w3, 1, 0.0811, 1005, datetime(2026, 10, 7, 2, 5, 0))                # the operator mints from Comet
    run(bot._check_manual_mint())

    assert bot.state == FSMState.ROLLOVER                                    # minted, converted, ready for the next step
    mint = [e for e in ledger.local_records if e.txn_type.value == "MINT"]
    assert len(mint) == 1 and mint[0].amount == pytest.approx(0.0811) and mint[0].external_ref == tx(1)
    assert db[rc.COLLECTION].docs[f"{tx(1)}:0"]["consumed"] is True


def test_the_run_ignores_a_mint_of_the_wrong_size():
    svc, w3, db = make()
    bot, ledger = fsm(svc)
    land(w3, 1, 0.363512, 995, datetime(2026, 10, 7, 2, 5, 0))
    run(bot._check_manual_mint())
    assert bot.state == FSMState.AWAITING_MANUAL_MINT
    assert not [e for e in ledger.local_records if e.txn_type.value == "MINT"]


def test_a_mint_from_before_the_run_is_booked_once_an_operator_attaches_it():
    svc, w3, db = make()
    bot, ledger = fsm(svc)
    h = land(w3, 1, 0.0811, 990, datetime(2026, 10, 6, 22, 21, 24))          # landed hours before the run
    run(bot._check_manual_mint())
    assert bot.state == FSMState.AWAITING_MANUAL_MINT                        # not auto-claimed

    run(svc.attach("RUN-A", h, 0.081081))                                    # operator pastes the hash
    run(bot._check_manual_mint())
    assert bot.state == FSMState.ROLLOVER
    assert [e for e in ledger.local_records if e.txn_type.value == "MINT"][0].external_ref == h


def test_without_receipts_the_original_balance_wait_still_works():
    """Simulations and tests with no database keep the original balance-delta behaviour."""
    ledger = ImmutableLedger(db_collection=None)
    balances = iter([4.0, 4.0811])

    async def swap_rate(a, b):
        return 1.0

    bot = HFTCorridorFSM(ledger, starting_capital_usd=0.0772, config={
        "node_procure": "N2", "node_liquidate": "N5", "discount": 0.05, "baseline_rate": 129.50, "run_id": "RUN-B",
        "poll_seconds": 0.001, "get_treasury_imc_balance_fn": lambda: next(balances),
        "get_swap_rate_fn": swap_rate, "get_treasury_usdt_balance_fn": lambda: 100.0,
    })
    assert bot._receipts is None
    bot.state = FSMState.AWAITING_MANUAL_MINT
    bot.pending_mint_expected_imc, bot.pending_mint_external_id, bot.pending_mint_treasury_imc_before = 0.081081, "X", 4.0
    run(bot._check_manual_mint())
    assert bot.state == FSMState.AWAITING_MANUAL_MINT          # first poll: balance unchanged
    run(bot._check_manual_mint())
    assert bot.state == FSMState.ROLLOVER                      # second poll: it rose by the expected amount


# ------------------------------------------------------------------ linking a receipt to a ledger entry that already used it
def test_mark_booked_links_a_receipt_to_the_run_and_ledger_entry_that_used_it():
    svc, w3, db = make()
    h = land(w3, 1, 0.363512, 990, T0)
    run(svc.scan())
    rec = run(svc.mark_booked(f"{h}:0", "RUN-OLD", "TXN-E13D402C", "booked by the earlier wait"))
    assert rec["claimedByRun"] == "RUN-OLD" and rec["consumed"] is True and rec["ledgerTxnId"] == "TXN-E13D402C"
    # and it can no longer be reused, by scan-claim or by attach
    assert run(svc.claim_match("RUN-NEW", 0.363512, None)) is None
    with pytest.raises(rc.AttachError, match="already used by run RUN-OLD"):
        run(svc.attach("RUN-NEW", h, 0.363512))


def test_mark_booked_refuses_unknown_and_already_used_receipts():
    svc, w3, db = make()
    with pytest.raises(LookupError):
        run(svc.mark_booked("0xnope:0", "RUN-OLD", "TXN-1", "x"))
    h = land(w3, 1, 0.0811, 990, T0)
    run(svc.scan())
    run(svc.claim_match("RUN-A", 0.0811, None))
    with pytest.raises(ValueError, match="already reserved for RUN-A"):
        run(svc.mark_booked(f"{h}:0", "RUN-OLD", "TXN-1", "x"))


# ------------------------------------------------------------------ using IMC the treasury already holds
def held(balance=4.693029, booked=0.363512, db=None):
    async def booked_fn():
        return booked
    return rc.ImcReceipts(db or DB(), w3=W3(), token=TOKEN, treasury=TREASURY, balance_fn=lambda: balance, booked_fn=booked_fn)


def test_free_imc_is_wallet_minus_booked_minus_reserved():
    svc = held()
    s = run(svc.free_imc())
    assert s["balance"] == pytest.approx(4.693029) and s["booked"] == pytest.approx(0.363512)
    assert s["free"] == pytest.approx(4.329517)


def test_allocating_held_imc_reserves_exactly_what_the_run_expects():
    svc = held()
    rec = run(svc.allocate_held("RUN-A", 0.081081))
    assert rec["amountImc"] == 0.081081 and rec["claimedByRun"] == "RUN-A" and rec["source"] == "held" and rec["consumed"] is False
    assert run(svc.free_imc())["free"] == pytest.approx(4.329517 - 0.081081)     # now counted as reserved
    assert run(svc.allocate_held("RUN-A", 0.081081))["_id"] == "HELD-RUN-A"        # idempotent: no second reservation
    assert run(svc.free_imc())["free"] == pytest.approx(4.329517 - 0.081081)


def test_held_imc_is_refused_when_not_enough_is_free_and_says_how_much():
    svc = held(balance=0.5, booked=0.45)              # only 0.05 free
    with pytest.raises(rc.HeldError) as e:
        run(svc.allocate_held("RUN-A", 0.081081))
    assert "0.050000" in str(e.value) and "0.081081" in str(e.value)


def test_two_runs_cannot_both_take_the_last_of_the_free_imc():
    svc = held(balance=0.5, booked=0.363512)          # 0.136488 free
    run(svc.allocate_held("RUN-A", 0.1))
    with pytest.raises(rc.HeldError):
        run(svc.allocate_held("RUN-B", 0.1))           # only 0.036488 left after RUN-A's reservation


def test_the_run_books_held_imc_and_continues_without_any_mint():
    db = DB()
    svc = held(db=db)
    bot, ledger = fsm(svc)
    run(bot._check_manual_mint())
    assert bot.state == FSMState.AWAITING_MANUAL_MINT                      # nothing minted, nothing allocated yet

    run(svc.allocate_held("RUN-A", 0.081081))                              # operator: use IMC already held
    run(bot._check_manual_mint())

    assert bot.state == FSMState.ROLLOVER
    (mint,) = [e for e in ledger.local_records if e.txn_type.value == "MINT"]
    assert mint.amount == pytest.approx(0.081081) and mint.external_ref == "HELD-RUN-A" and mint.vault_backed is True
    assert db[rc.COLLECTION].docs["HELD-RUN-A"]["consumed"] is True


def test_releasing_a_run_frees_held_imc_and_unclaims_chain_transfers():
    db = DB()
    svc = held(db=db)
    run(svc.allocate_held("RUN-A", 0.081081))
    s2, w3, _ = make(db=db)
    h = land(w3, 1, 0.5, 1005, T0)
    w3.eth.block_number = 1010
    run(s2.scan())
    run(s2.claim_match("RUN-A", 0.5, None))
    assert run(svc.release_run("RUN-A")) == 2
    assert "HELD-RUN-A" not in db[rc.COLLECTION].docs                       # held reservation is gone
    chain = db[rc.COLLECTION].docs[f"{h}:0"]
    assert chain["claimedByRun"] is None                                    # the real transfer is claimable again
    assert run(svc.free_imc())["free"] == pytest.approx(4.329517)
