"""Minting IMC against a top-up receipt through Comet's booking endpoints. Comet and Celo are in-memory doubles:
no mint, no transfer, no network."""
import asyncio
import json

import pytest

from services import imc_booking as ib
from services.comet_client import CometClient

TREASURY = "0x6f7BeAb48EAfC47B89041899a35a0525a6A60F59"


def run(coro):
    return asyncio.run(coro)


class Col:
    def __init__(self):
        self.docs = {}

    async def find_one(self, q, proj=None):
        d = self.docs.get(q.get("_id"))
        return dict(d) if d else None

    async def update_one(self, q, upd, upsert=False):
        d = self.docs.setdefault(q["_id"], dict(upd.get("$setOnInsert", {})) if upsert else {})
        d.update(upd.get("$set", {}))

    def find(self, q):
        return self

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        return self

    async def to_list(self, length):
        return [{"_id": k, **v} for k, v in self.docs.items()]


class DB(dict):
    def __init__(self):
        super().__init__(imc_bookings=Col())


class FakeComet:
    def __init__(self, response=None, treasury=TREASURY):
        self.calls = []
        self.response = response
        self.treasury = treasury

    def book_imc(self, receipt, cash_kes=None, chain="celo"):
        self.calls.append(("book", receipt, cash_kes, chain))
        return self.response or {"status": "success", "data": {"receipt": receipt, "txHash": "0xMINT", "treasury": self.treasury,
                                                               "preview": {"airtimeKes": 10.5, "imc": 0.081081, "usdKes": 129.5}}}

    def book_and_send(self, receipt, to_address, cash_kes=None, chain="celo"):
        self.calls.append(("book-and-send", receipt, to_address, cash_kes, chain))
        return self.response or {"status": "success", "data": {"receipt": receipt, "txHash": "0xMINT", "sendTxHash": "0xSEND", "to": to_address,
                                                               "amount": 0.081081, "treasury": self.treasury,
                                                               "preview": {"airtimeKes": 10.5, "imc": 0.081081, "usdKes": 129.5}}}


class FakeReceipts:
    """Stands in for ImcReceipts: which transactions show an IMC transfer into the treasury."""

    def __init__(self, seen=None):
        self.seen, self.asked = seen or {}, []

    def __call__(self, db):
        return self

    async def record_tx(self, tx):
        self.asked.append(tx)
        if tx not in self.seen:
            raise ValueError("not found yet")
        return [{"amountImc": self.seen[tx]}]


def deps(comet, receipts, db=None):
    return ib.Deps(comet=comet, db=db if db is not None else DB(), receipts=receipts, treasury_address=TREASURY, poll_attempts=2, poll_seconds=0.001)


def book(d, receipt="UJ7ABC1234", cash=10, to=None, steps=None):
    return run(ib.book(d, receipt, cash, to, "admin-1", on_step=(steps.append if steps is not None else None)))


# --------------------------------------------------------------------------------------------- the request to Comet
def test_the_client_uses_the_tenants_own_api_key_and_secret_not_a_service_token(monkeypatch):
    monkeypatch.setenv("COMET_API_KEY", "key1")
    monkeypatch.setenv("COMET_API_SECRET", "sec1")
    c = CometClient()
    seen = {}
    monkeypatch.setattr(c, "_post", lambda path, payload, use_service_token=False, timeout=15: seen.update(path=path, payload=payload, tok=use_service_token, timeout=timeout) or {"status": "success", "data": {}})
    c.book_imc("UJ7ABC1234", 10)
    assert seen["path"] == "/api/v1/imc/book?chain=celo" and seen["payload"] == {"receipt": "UJ7ABC1234", "cashKes": 10}
    assert seen["tok"] is False and seen["timeout"] > 15                      # API key + secret, and room for Celo to confirm
    h = c._headers(False)
    assert h["X-API-Key"] == "key1" and h["X-API-Secret"] == "sec1" and "X-Service-Token" not in h
    c.book_and_send("R-1234", "0xABC", None)
    assert seen["path"] == "/api/v1/imc/book-and-send?chain=celo" and seen["payload"] == {"receipt": "R-1234", "toAddress": "0xABC"}   # cashKes omitted when not given


# --------------------------------------------------------------------------------------------- validation
@pytest.mark.parametrize("bad", ["", "  ", "ab", "has space", "semi;colon", "x" * 129, "../../etc"])
def test_a_malformed_receipt_is_refused_before_anything_is_sent(bad):
    with pytest.raises(ib.BookingError, match="receipt"):
        ib.clean_receipt(bad)


@pytest.mark.parametrize("bad", [0, -5, 2_000_000, True, "10"])
def test_cash_must_be_a_sensible_number(bad):
    with pytest.raises(ib.BookingError, match="Cash"):
        ib.clean_cash(bad)


def test_the_delivery_address_is_checked_and_checksummed():
    assert ib.clean_address("") is None and ib.clean_address(None) is None
    assert ib.clean_address(TREASURY.lower()) == TREASURY
    with pytest.raises(ib.BookingError, match="not a valid Celo address"):
        ib.clean_address("0x123")


# --------------------------------------------------------------------------------------------- booking
def test_a_good_receipt_is_booked_and_the_arrival_is_confirmed_on_celo():
    comet, rec, steps = FakeComet(), FakeReceipts({"0xMINT": 0.081081}), []
    d = deps(comet, rec)
    out = book(d, steps=steps)
    assert comet.calls == [("book", "UJ7ABC1234", 10, "celo")]
    assert out["txHash"] == "0xMINT" and out["arrivedImc"] == pytest.approx(0.081081) and out["warning"] is None
    assert rec.asked == ["0xMINT"]                                           # it checked the mint transaction on Celo
    saved = run(d.db["imc_bookings"].find_one({"_id": "UJ7ABC1234"}))
    assert saved["status"] == "booked" and saved["by"] == "admin-1" and saved["arrivedImc"] == pytest.approx(0.081081)
    assert any("Waiting for the IMC" in s for s in steps)


def test_a_receipt_booked_before_is_refused_locally_without_calling_comet():
    comet, d = FakeComet(), deps(FakeComet(), FakeReceipts({"0xMINT": 0.081081}))
    book(d)
    comet2 = FakeComet()
    d2 = deps(comet2, FakeReceipts(), d.db)
    with pytest.raises(ib.BookingError, match="already booked.*0xMINT"):
        book(d2)
    assert comet2.calls == []                                                  # one receipt books once, ever


def test_a_409_with_the_original_hash_is_recovered_not_lost():
    body = json.dumps({"error": "receipt already booked", "txHash": "0xORIG"})
    comet = FakeComet({"status": "error", "message": body, "statusCode": 409})
    out = book(deps(comet, FakeReceipts({"0xORIG": 0.081081})))
    assert out["txHash"] == "0xORIG" and out["recovered"] is True and out["arrivedImc"] == pytest.approx(0.081081)


def test_a_409_still_confirming_asks_the_operator_to_wait_and_allows_a_retry():
    comet = FakeComet({"status": "error", "message": json.dumps({"error": "mint still confirming"}), "statusCode": 409})
    d = deps(comet, FakeReceipts())
    with pytest.raises(ib.BookingError, match="still confirming"):
        book(d)
    assert run(d.db["imc_bookings"].find_one({"_id": "UJ7ABC1234"}))["status"] == "pending"     # not 'booked': the same receipt may be retried


def test_a_refusal_from_comet_is_shown_with_its_reason_and_the_receipt_stays_retryable():
    comet = FakeComet({"status": "error", "message": json.dumps({"error": "not enough airtime backing to mint IMC"}), "statusCode": 502})
    d = deps(comet, FakeReceipts())
    with pytest.raises(ib.BookingError, match="not enough airtime backing"):
        book(d)
    assert run(d.db["imc_bookings"].find_one({"_id": "UJ7ABC1234"}))["status"] == "failed"
    comet2 = FakeComet()                                                       # the operator fixes the float and tries again
    assert book(deps(comet2, FakeReceipts({"0xMINT": 0.081081}), d.db))["txHash"] == "0xMINT"


def test_a_403_explains_that_the_tenants_own_key_is_needed():
    comet = FakeComet({"status": "error", "message": "forbidden", "statusCode": 403})
    with pytest.raises(ib.BookingError, match="tenant's own"):
        book(deps(comet, FakeReceipts()))


def test_minted_but_not_delivered_tells_the_operator_to_repeat_the_same_request():
    comet = FakeComet({"status": "error", "statusCode": 502, "message": json.dumps({"error": "delivery failed", "minted": True, "txHash": "0xMINT"})})
    d = deps(comet, FakeReceipts())
    with pytest.raises(ib.BookingError, match="only repeats the delivery"):
        book(d, to=TREASURY)
    saved = run(d.db["imc_bookings"].find_one({"_id": "UJ7ABC1234"}))
    assert saved["status"] == "pending" and saved["minted"] is True and saved["txHash"] == "0xMINT"


def test_book_and_send_delivers_to_the_chosen_address_and_checks_the_send_transaction():
    other = "0xa68521ab3F603abE9Cc477220c84c94aa7941B65"
    comet, rec = FakeComet(), FakeReceipts({"0xSEND": 0.081081})
    out = book(deps(comet, rec), to=other)
    assert comet.calls == [("book-and-send", "UJ7ABC1234", other, 10, "celo")]
    assert out["sendTxHash"] == "0xSEND" and out["deliveredTo"] == other
    assert rec.asked == []                                                     # not our treasury, so no treasury arrival to wait for


def test_delivery_to_the_treasury_itself_is_confirmed_on_the_send_transaction():
    rec = FakeReceipts({"0xSEND": 0.081081})
    out = book(deps(FakeComet(), rec), to=TREASURY)
    assert rec.asked == ["0xSEND"] and out["arrivedImc"] == pytest.approx(0.081081)


def test_comet_minting_into_a_different_treasury_is_flagged_instead_of_waiting_forever():
    comet = FakeComet(treasury="0xd459aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaafa9b")
    rec = FakeReceipts()
    out = book(deps(comet, rec))
    assert "not your treasury wallet" in out["warning"] and rec.asked == []


def test_an_arrival_that_does_not_match_the_preview_is_flagged():
    out = book(deps(FakeComet(), FakeReceipts({"0xMINT": 0.5})))
    assert "arrived on Celo" in out["warning"]


def test_a_mint_not_yet_visible_on_celo_is_booked_with_a_clear_note():
    out = book(deps(FakeComet(), FakeReceipts()))                              # the chain never shows it within the polls
    assert out["arrivedImc"] is None and "not visible on Celo yet" in out["warning"]


# --------------------------------------------------------------------------------------------- the background job
def _wait(job_id):
    async def go():
        for _ in range(500):
            j = ib.latest_job()
            if j["id"] == job_id and j["status"] != "running":
                return j
            await asyncio.sleep(0.01)
        raise AssertionError("never finished")
    return go()


def test_start_returns_at_once_and_the_job_reports_its_steps_and_result():
    async def main():
        d = deps(FakeComet(), FakeReceipts({"0xMINT": 0.081081}))
        job = await ib.start(d, " UJ7ABC1234 ", 10, None, "admin-1")
        assert job["status"] == "running" and job["receipt"] == "UJ7ABC1234"
        return await _wait(job["id"])

    done = run(main())
    assert done["status"] == "done" and done["result"]["txHash"] == "0xMINT" and done["steps"][-1]["msg"] == "Done"


def test_a_bad_request_or_a_second_job_is_refused_up_front():
    async def main():
        with pytest.raises(ib.BookingError, match="receipt"):
            await ib.start(deps(FakeComet(), FakeReceipts()), "no", 10, None, "a")
        first = await ib.start(deps(FakeComet(), FakeReceipts({"0xMINT": 0.081081})), "UJ7AAAA111", 10, None, "a")
        with pytest.raises(ib.BookingError, match="already in progress"):
            await ib.start(deps(FakeComet(), FakeReceipts()), "UJ7BBBB222", 10, None, "a")
        await _wait(first["id"])

    run(main())


def test_a_failed_booking_ends_the_job_as_failed_and_frees_the_slot():
    async def main():
        bad = FakeComet({"status": "error", "message": json.dumps({"error": "gateway could not size the mint"}), "statusCode": 502})
        job = await ib.start(deps(bad, FakeReceipts()), "UJ7FAIL001", None, None, "a")
        failed = await _wait(job["id"])
        assert failed["status"] == "failed" and "gateway could not size" in failed["error"]
        ok = await ib.start(deps(FakeComet(), FakeReceipts({"0xMINT": 0.081081})), "UJ7OKAY002", None, None, "a")
        return await _wait(ok["id"])

    assert run(main())["status"] == "done"


# --------------------------------------------------------------------------------------------- 2026-10-07: what the first live bookings showed
def test_by_default_the_mint_is_sent_on_to_our_own_wallet():
    """A plain /imc/book minted 0.151743 IMC into Comet's treasury (0xa4ec…), not into our master wallet."""
    async def main():
        comet = FakeComet()
        d = deps(comet, FakeReceipts({"0xSEND": 0.081081}))
        job = await ib.start(d, "UJ7DEFAULT1", None, None, "admin-1")        # no delivery address given
        done = await _wait(job["id"])
        return comet, done

    comet, done = run(main())
    assert comet.calls == [("book-and-send", "UJ7DEFAULT1", TREASURY, None, "celo")]       # delivered to our wallet, cashKes not sent
    assert done["status"] == "done" and done["result"]["arrivedImc"] == pytest.approx(0.081081)


def test_nothing_to_mint_is_a_clear_final_answer_not_a_wait():
    body = json.dumps({"error": "nothing to book: every KES of airtime in the float is already issued as IMC (float 1711.50 KES, issued 1711.50 KES)"})
    comet = FakeComet({"status": "error", "message": body, "statusCode": 409})
    d = deps(comet, FakeReceipts())
    with pytest.raises(ib.BookingError) as e:
        book(d)
    assert "Nothing to mint" in str(e.value) and "Top up first" in str(e.value) and "still confirming" not in str(e.value)
    assert run(d.db["imc_bookings"].find_one({"_id": "UJ7ABC1234"}))["status"] == "nothing"


def test_a_mint_left_in_comets_treasury_can_be_delivered_to_our_wallet_once():
    d = deps(FakeComet(), FakeReceipts({"0xSEND": 0.151743}))
    run(d.db["imc_bookings"].update_one({"_id": "UJ7HD9ITVC"}, {"$set": {"status": "booked", "txHash": "0xMINT", "treasury": "0xa4ec", "imc": 0.151743, "arrivedImc": None, "sendTxHash": None}}, upsert=True))
    steps = []
    out = book(d, receipt="UJ7HD9ITVC", cash=None, to=TREASURY, steps=steps)
    assert any("already minted into Comet's treasury" in s for s in steps)
    assert out["sendTxHash"] == "0xSEND" and out["arrivedImc"] == pytest.approx(0.151743)
    with pytest.raises(ib.BookingError, match="already booked"):                           # now delivered: it cannot be delivered twice
        book(deps(FakeComet(), FakeReceipts(), d.db), receipt="UJ7HD9ITVC", cash=None, to=TREASURY)


def test_if_comet_says_already_booked_and_sends_nothing_we_say_the_imc_was_not_delivered():
    body = json.dumps({"error": "receipt already booked", "txHash": "0xMINT"})
    comet = FakeComet({"status": "error", "message": body, "statusCode": 409})
    with pytest.raises(ib.BookingError, match="did not send the IMC to your wallet"):
        book(deps(comet, FakeReceipts()), to=TREASURY)
