from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .accounts import APPROVED_ACCOUNT_SET


class DocumentType(str, Enum):
    RECEIPT = "receipt"
    INVOICE = "invoice"
    PROOF_OF_PAYMENT = "proof_of_payment"
    INCOME_RECEIPT = "income_receipt"
    BANK_STATEMENT_EXTRACT = "bank_statement_extract"
    UNKNOWN = "unknown"


class Direction(str, Enum):
    EXPENSE = "Expense"
    INCOME = "Income"


class ReceiptExtraction(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    document_type: DocumentType = DocumentType.UNKNOWN
    merchant_name: str | None = None
    transaction_date: date | None = None
    total_amount_zar: Decimal | None = None
    currency: str = "ZAR"
    vat_amount: Decimal | None = None
    payment_method: str | None = None
    invoice_or_receipt_number: str | None = None
    line_items_summary: str = ""
    direction: Direction = Direction.EXPENSE
    account: str | None = None
    notes: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_user_review: bool = True
    reasoning_summary: str = ""

    @field_validator("transaction_date", mode="before")
    @classmethod
    def parse_transaction_date(cls, value: Any) -> date | None:
        if value in (None, ""):
            return None
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    @field_validator("total_amount_zar", "vat_amount", mode="before")
    @classmethod
    def parse_decimal(cls, value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: Any) -> str:
        return "ZAR" if value in (None, "") else str(value).upper()

    @field_validator("document_type", mode="before")
    @classmethod
    def normalize_document_type(cls, value: Any) -> DocumentType:
        if value in (None, ""):
            return DocumentType.UNKNOWN
        if isinstance(value, DocumentType):
            return value
        return DocumentType(str(value).strip().lower())

    @field_validator("direction", mode="before")
    @classmethod
    def normalize_direction(cls, value: Any) -> Direction:
        if isinstance(value, Direction):
            return value
        lowered = str(value).strip().lower()
        return Direction.EXPENSE if lowered == "expense" else Direction.INCOME

    @field_validator("line_items_summary", "notes", "reasoning_summary", mode="before")
    @classmethod
    def normalize_text_fields(cls, value: Any) -> str:
        return "" if value is None else str(value)


class ProposedTransaction(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    transaction_date: date
    direction: Direction
    account: str
    amount: Decimal
    notes: str
    confidence: float = Field(ge=0.0, le=1.0)
    needs_user_review: bool = True
    reasoning_summary: str = ""
    merchant_name: str | None = None
    document_type: DocumentType = DocumentType.UNKNOWN

    @field_validator("amount", mode="before")
    @classmethod
    def parse_amount(cls, value: Any) -> Decimal:
        return value if isinstance(value, Decimal) else Decimal(str(value))

    @model_validator(mode="after")
    def validate_account(self) -> "ProposedTransaction":
        if self.account not in APPROVED_ACCOUNT_SET:
            raise ValueError("account must be an approved account name")
        return self

    def preview_text(self) -> str:
        confidence_label = (
            "High" if self.confidence >= 0.85 else "Medium" if self.confidence >= 0.65 else "Low"
        )
        return "\n".join(
            [
                "Proposed transaction:",
                f"Date: {self.transaction_date.isoformat()}",
                f"Direction: {self.direction}",
                f"Account: {self.account}",
                f"Amount: R{self.amount:.2f}",
                f"Notes: {self.notes}",
                f"Confidence: {confidence_label}",
                f"Review required: {'Yes' if self.needs_user_review else 'No'}",
            ]
        )


@dataclass(slots=True)
class UploadedReceipt:
    telegram_user_id: int
    file_name: str
    file_path: Path
    mime_type: str
    telegram_file_id: str
    file_size: int


@dataclass(slots=True)
class PendingReceipt:
    upload: UploadedReceipt
    extraction: ReceiptExtraction
    proposed: ProposedTransaction
    audit_id: int | None = None


@dataclass(slots=True)
class UploadProcessResult:
    pending: PendingReceipt
    queued: bool = False
    queue_position: int = 1


@dataclass(slots=True)
class PendingTransition:
    current: PendingReceipt | None
    queue_length: int = 0


@dataclass(slots=True)
class ConfirmResult:
    write_result: WriteResult
    next_pending: PendingReceipt | None = None
    queue_length: int = 0


@dataclass(slots=True)
class UserSession:
    dry_run: bool
    batch_mode: bool
    pending: PendingReceipt | None = None
    pending_queue: list[PendingReceipt] = field(default_factory=list)
    edit_field: str | None = None
    undo_candidate_audit_id: int | None = None
    last_completed_audit_id: int | None = None
    last_preview_path: Path | None = None
    last_export_path: Path | None = None
    rate_window: list[datetime] = field(default_factory=list)


@dataclass(slots=True)
class WriteResult:
    workbook_path: Path
    backup_path: Path
    output_path: Path | None
    workbook_row_number: int
    preview: bool = False
