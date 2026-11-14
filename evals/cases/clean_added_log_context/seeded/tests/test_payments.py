from app.payments import record_payment


def test_a_recorded_payment_is_appended_to_the_ledger() -> None:
    ledger: list[tuple[str, int]] = [("p-1", 500)]

    record_payment(ledger, "p-2", 1250)

    assert ledger == [("p-1", 500), ("p-2", 1250)]
