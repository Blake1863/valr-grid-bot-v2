from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .accounts import APPROVED_ACCOUNT_SET
from .audit_log import AuditLog
from .classifier import ReceiptClassifier
from .models import (
    ConfirmResult,
    PendingReceipt,
    PendingTransition,
    ProposedTransaction,
    ReceiptExtraction,
    UploadProcessResult,
    UploadedReceipt,
    UserSession,
    WriteResult,
)
from .ocr_client import OpenAIReceiptOCRClient
from .workbook_writer import WorkbookWriter


class ReceiptBotController:
    def __init__(
        self,
        *,
        ocr_client: OpenAIReceiptOCRClient,
        classifier: ReceiptClassifier,
        workbook_writer: WorkbookWriter,
        audit_log: AuditLog,
        authorized_user_ids: set[int],
        default_dry_run: bool,
        default_batch_mode: bool,
        rate_limit_window_seconds: int,
        rate_limit_max_uploads: int,
    ) -> None:
        self.ocr_client = ocr_client
        self.classifier = classifier
        self.workbook_writer = workbook_writer
        self.audit_log = audit_log
        self.authorized_user_ids = authorized_user_ids
        self.default_dry_run = default_dry_run
        self.default_batch_mode = default_batch_mode
        self.rate_limit_window_seconds = rate_limit_window_seconds
        self.rate_limit_max_uploads = rate_limit_max_uploads
        self.sessions: dict[int, UserSession] = {}

    def ensure_authorized(self, telegram_user_id: int) -> None:
        if telegram_user_id not in self.authorized_user_ids:
            raise PermissionError("You are not authorized to use this bot.")

    def get_session(self, telegram_user_id: int) -> UserSession:
        session = self.sessions.get(telegram_user_id)
        if session is None:
            session = UserSession(dry_run=self.default_dry_run, batch_mode=self.default_batch_mode)
            self.sessions[telegram_user_id] = session
        return session

    def register_upload(self, telegram_user_id: int) -> None:
        session = self.get_session(telegram_user_id)
        now = datetime.now(UTC)
        session.rate_window = [
            timestamp
            for timestamp in session.rate_window
            if (now - timestamp).total_seconds() <= self.rate_limit_window_seconds
        ]
        if len(session.rate_window) >= self.rate_limit_max_uploads:
            raise ValueError("Rate limit reached. Wait a minute and try again.")
        session.rate_window.append(now)

    def set_dry_run(self, telegram_user_id: int, enabled: bool) -> UserSession:
        session = self.get_session(telegram_user_id)
        session.dry_run = enabled
        if not enabled:
            session.last_preview_path = None
        return session

    def set_batch_mode(self, telegram_user_id: int, enabled: bool) -> UserSession:
        session = self.get_session(telegram_user_id)
        session.batch_mode = enabled
        return session

    def process_upload(self, upload: UploadedReceipt) -> UploadProcessResult:
        self.ensure_authorized(upload.telegram_user_id)
        self.register_upload(upload.telegram_user_id)
        extraction = self.ocr_client.extract(upload.file_path, upload.mime_type)
        proposed = self.classifier.classify(extraction)
        audit_id = self.audit_log.create_entry(
            telegram_user_id=upload.telegram_user_id,
            original_file_name=upload.file_name,
            document_type=str(extraction.document_type),
            extracted_json=extraction.model_dump(mode="json"),
            final_row_json=proposed.model_dump(mode="json"),
            status="pending",
        )
        pending = PendingReceipt(upload=upload, extraction=extraction, proposed=proposed, audit_id=audit_id)
        session = self.get_session(upload.telegram_user_id)
        if session.pending is None:
            session.pending = pending
            session.edit_field = None
            return UploadProcessResult(pending=pending, queued=False, queue_position=1)

        session.pending_queue.append(pending)
        return UploadProcessResult(
            pending=pending,
            queued=True,
            queue_position=len(session.pending_queue) + 1,
        )

    def begin_edit(self, telegram_user_id: int, field_name: str) -> None:
        session = self.get_session(telegram_user_id)
        if session.pending is None:
            raise ValueError("There is no pending transaction to edit.")
        session.edit_field = field_name

    def apply_edit(self, telegram_user_id: int, value: str) -> PendingReceipt:
        session = self.get_session(telegram_user_id)
        if session.pending is None or session.edit_field is None:
            raise ValueError("There is no pending edit waiting for input.")

        proposed = session.pending.proposed.model_copy(deep=True)
        field_name = session.edit_field
        if field_name == "transaction_date":
            proposed.transaction_date = datetime.fromisoformat(value).date()
        elif field_name == "amount":
            proposed.amount = Decimal(value)
        elif field_name == "account":
            if value not in APPROVED_ACCOUNT_SET:
                raise ValueError("That account is not in the approved account list.")
            proposed.account = value
        elif field_name == "notes":
            proposed.notes = value.strip()
        else:
            raise ValueError("Unsupported edit field.")

        session.pending = PendingReceipt(
            upload=session.pending.upload,
            extraction=session.pending.extraction,
            proposed=ProposedTransaction.model_validate(proposed.model_dump(mode="json")),
            audit_id=session.pending.audit_id,
        )
        session.edit_field = None
        return session.pending

    def cancel_pending(self, telegram_user_id: int) -> PendingTransition:
        session = self.get_session(telegram_user_id)
        if session.pending and session.pending.audit_id:
            self.audit_log.update_entry(session.pending.audit_id, status="cancelled")
        session.pending = self._promote_next_pending(session)
        session.edit_field = None
        return PendingTransition(current=session.pending, queue_length=len(session.pending_queue))

    def confirm_pending(self, telegram_user_id: int) -> ConfirmResult:
        session = self.get_session(telegram_user_id)
        if session.pending is None or session.pending.audit_id is None:
            raise ValueError("There is no pending transaction to confirm.")

        pending = session.pending
        result = (
            self.workbook_writer.create_preview(pending.proposed)
            if session.dry_run
            else self.workbook_writer.append_transaction(pending.proposed)
        )
        session.last_preview_path = result.output_path if result.preview else None
        status = "previewed" if result.preview else "written"
        self.audit_log.update_entry(
            pending.audit_id,
            final_row_json=pending.proposed.model_dump(mode="json"),
            workbook_row_number=result.workbook_row_number,
            workbook_output_path=str(result.workbook_path),
            status=status,
        )
        session.last_completed_audit_id = pending.audit_id
        session.pending = self._promote_next_pending(session)
        session.edit_field = None
        return ConfirmResult(
            write_result=result,
            next_pending=session.pending,
            queue_length=len(session.pending_queue),
        )

    def create_backup(self, telegram_user_id: int) -> Path:
        self.ensure_authorized(telegram_user_id)
        return self.workbook_writer.create_backup()

    def export_latest(self, telegram_user_id: int, *, prefer_preview: bool = False) -> Path:
        self.ensure_authorized(telegram_user_id)
        session = self.get_session(telegram_user_id)
        source = session.last_preview_path if prefer_preview and session.last_preview_path else None
        export_path = self.workbook_writer.export_workbook(source_path=source)
        session.last_export_path = export_path
        if session.last_completed_audit_id is not None:
            self.audit_log.update_entry(
                session.last_completed_audit_id,
                workbook_output_path=str(export_path),
                exported=True,
            )
        return export_path

    def list_recent_transactions(self, telegram_user_id: int, limit: int = 5) -> list[dict[str, str]]:
        self.ensure_authorized(telegram_user_id)
        rows = self.audit_log.list_recent(telegram_user_id, limit)
        result: list[dict[str, str]] = []
        for row in rows:
            final_row = json.loads(row["final_row_json"]) if row["final_row_json"] else {}
            result.append(
                {
                    "id": str(row["id"]),
                    "date": str(final_row.get("transaction_date", "")),
                    "direction": str(final_row.get("direction", "")),
                    "account": str(final_row.get("account", "")),
                    "amount": str(final_row.get("amount", "")),
                    "notes": str(final_row.get("notes", "")),
                    "status": str(row["status"]),
                }
            )
        return result

    def prepare_undo(self, telegram_user_id: int) -> dict[str, str]:
        self.ensure_authorized(telegram_user_id)
        session = self.get_session(telegram_user_id)
        row = self.audit_log.last_written_entry(telegram_user_id)
        if row is None:
            raise ValueError("There is no recent bot-written transaction to undo.")
        session.undo_candidate_audit_id = int(row["id"])
        final_row = json.loads(row["final_row_json"]) if row["final_row_json"] else {}
        return {
            "id": str(row["id"]),
            "date": str(final_row.get("transaction_date", "")),
            "direction": str(final_row.get("direction", "")),
            "account": str(final_row.get("account", "")),
            "amount": str(final_row.get("amount", "")),
            "notes": str(final_row.get("notes", "")),
        }

    def confirm_undo(self, telegram_user_id: int) -> Path:
        session = self.get_session(telegram_user_id)
        if session.undo_candidate_audit_id is None:
            raise ValueError("There is no undo waiting for confirmation.")
        row = self.audit_log.get_entry(session.undo_candidate_audit_id)
        if row is None or row["final_row_json"] is None or row["workbook_row_number"] is None:
            raise ValueError("Undo target could not be found.")
        final_row = json.loads(row["final_row_json"])
        expected_values = {
            "Date": str(final_row["transaction_date"]),
            "Direction": str(final_row["direction"]),
            "Account": str(final_row["account"]),
            "Amount": f"{Decimal(str(final_row['amount'])):.2f}",
            "Notes": str(final_row["notes"]),
        }
        backup_path = self.workbook_writer.undo_last_transaction(
            workbook_row_number=int(row["workbook_row_number"]),
            expected_values=expected_values,
        )
        self.audit_log.update_entry(session.undo_candidate_audit_id, status="undone", undone=True)
        session.undo_candidate_audit_id = None
        return backup_path

    def _promote_next_pending(self, session: UserSession) -> PendingReceipt | None:
        if not session.pending_queue:
            return None
        return session.pending_queue.pop(0)
