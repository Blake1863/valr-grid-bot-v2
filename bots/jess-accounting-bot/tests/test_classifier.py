from __future__ import annotations

from datetime import date
from decimal import Decimal

from bot.classifier import ReceiptClassifier
from bot.models import Direction, DocumentType, ReceiptExtraction


def make_extraction(merchant: str, summary: str, *, direction: Direction = Direction.EXPENSE) -> ReceiptExtraction:
    return ReceiptExtraction(
        document_type=DocumentType.RECEIPT,
        merchant_name=merchant,
        transaction_date=date(2026, 6, 10),
        total_amount_zar=Decimal("100.00"),
        currency="ZAR",
        line_items_summary=summary,
        direction=direction,
        account=None,
        notes="",
        confidence=0.9,
        needs_user_review=False,
        reasoning_summary="",
    )


def test_petrol_receipt_maps_to_petrol() -> None:
    proposed = ReceiptClassifier().classify(make_extraction("Shell", "Petrol purchase"))
    assert proposed.account == "Petrol"


def test_phone_bill_maps_to_phone() -> None:
    proposed = ReceiptClassifier().classify(make_extraction("Vodacom", "Monthly mobile bill"))
    assert proposed.account == "Phone"


def test_google_storage_maps_correctly() -> None:
    proposed = ReceiptClassifier().classify(make_extraction("Google One", "Cloud storage"))
    assert proposed.account == "Google Storage"


def test_costume_receipt_maps_correctly() -> None:
    proposed = ReceiptClassifier().classify(make_extraction("Dance Boutique", "Costumes and props"))
    assert proposed.account == "Costumes and Props"


def test_contractor_payment_maps_correctly() -> None:
    proposed = ReceiptClassifier().classify(make_extraction("EFT Proof", "Payment to Reese"))
    assert proposed.account == "Payment to contractor - Reese"


def test_unknown_receipt_requires_review() -> None:
    proposed = ReceiptClassifier().classify(make_extraction("Unknown Merchant", "Miscellaneous"))
    assert proposed.needs_user_review is True


def test_income_defaults_to_fees() -> None:
    proposed = ReceiptClassifier().classify(make_extraction("Jess Dance", "Student fees", direction=Direction.INCOME))
    assert proposed.account == "Fees earned"


def test_transport_alias_maps_to_motor_vehicle_and_requires_review() -> None:
    extraction = make_extraction(
        "Kejole Transport Services",
        "Transport from The Courtyard Hotel to 23 Noupit Avenue, Roodepoort",
    ).model_copy(update={"account": "Transport Expense"})
    proposed = ReceiptClassifier().classify(extraction)
    assert proposed.account == "Motor Vehicle Expense"
    assert proposed.needs_user_review is True


def test_roodepoort_address_does_not_trigger_festival_account_without_festival_context() -> None:
    proposed = ReceiptClassifier().classify(
        make_extraction(
            "Kejole Transport Services",
            "Transport from The Courtyard Hotel to 23 Noupit Avenue, Roodepoort",
        )
    )
    assert proposed.account == "Motor Vehicle Expense"
    assert proposed.account != "Roodepoort Dance Festival Entries"


def test_roodepoort_festival_requires_both_keywords() -> None:
    proposed = ReceiptClassifier().classify(
        make_extraction(
            "Roodepoort Festival",
            "Entry fees for Roodepoort festival competition",
        )
    )
    assert proposed.account == "Roodepoort Dance Festival Entries"
