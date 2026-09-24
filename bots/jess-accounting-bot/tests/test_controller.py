from __future__ import annotations

from pathlib import Path

import pytest

from bot.models import UploadedReceipt


def make_upload(tmp_path: Path, *, user_id: int = 12345, file_name: str = "receipt.pdf", mime_type: str = "application/pdf") -> UploadedReceipt:
    path = tmp_path / file_name
    path.write_bytes(b"fake")
    return UploadedReceipt(
        telegram_user_id=user_id,
        file_name=file_name,
        file_path=path,
        mime_type=mime_type,
        telegram_file_id="tg-file-id",
        file_size=4,
    )


def test_unauthorized_user_rejected(controller, tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        controller.process_upload(make_upload(tmp_path, user_id=999))


def test_image_upload_creates_pending_transaction(controller, tmp_path: Path) -> None:
    result = controller.process_upload(make_upload(tmp_path, file_name="receipt.jpg", mime_type="image/jpeg"))
    assert result.pending.proposed.account == "Petrol"
    assert result.queued is False
    assert controller.get_session(12345).pending is not None


def test_pdf_upload_creates_pending_transaction(controller, tmp_path: Path) -> None:
    result = controller.process_upload(make_upload(tmp_path, file_name="receipt.pdf", mime_type="application/pdf"))
    assert result.pending.proposed.notes == "Shell - petrol"


def test_confirm_writes_transaction(controller, tmp_path: Path) -> None:
    controller.process_upload(make_upload(tmp_path))
    result = controller.confirm_pending(12345)
    assert result.write_result.workbook_row_number == 4


def test_edit_account_updates_pending_transaction(controller, tmp_path: Path) -> None:
    controller.process_upload(make_upload(tmp_path))
    controller.begin_edit(12345, "account")
    pending = controller.apply_edit(12345, "Phone")
    assert pending.proposed.account == "Phone"


def test_cancel_does_not_write(controller, tmp_path: Path) -> None:
    controller.process_upload(make_upload(tmp_path))
    controller.cancel_pending(12345)
    assert controller.get_session(12345).pending is None


def test_export_returns_latest_workbook(controller, tmp_path: Path) -> None:
    controller.process_upload(make_upload(tmp_path))
    controller.confirm_pending(12345)
    export_path = controller.export_latest(12345)
    assert export_path.exists()


def test_dry_run_does_not_write_to_production(controller, tmp_path: Path) -> None:
    controller.set_dry_run(12345, True)
    controller.process_upload(make_upload(tmp_path))
    result = controller.confirm_pending(12345)
    assert result.write_result.preview is True
    raw = controller.workbook_writer._get_raw_sheet(__import__("openpyxl").load_workbook(controller.workbook_writer.workbook_path))
    assert raw["C4"].value is None


def test_batch_mode_exports_only_when_requested(controller, tmp_path: Path) -> None:
    controller.set_batch_mode(12345, True)
    controller.process_upload(make_upload(tmp_path))
    controller.confirm_pending(12345)
    assert controller.get_session(12345).last_export_path is None
    export_path = controller.export_latest(12345)
    assert export_path.exists()


def test_second_upload_is_queued_until_first_is_resolved(controller, tmp_path: Path) -> None:
    first = controller.process_upload(make_upload(tmp_path, file_name="one.pdf"))
    second = controller.process_upload(make_upload(tmp_path, file_name="two.pdf"))

    session = controller.get_session(12345)
    assert first.queued is False
    assert second.queued is True
    assert second.queue_position == 2
    assert session.pending is first.pending
    assert len(session.pending_queue) == 1


def test_confirm_advances_to_next_queued_receipt(controller, tmp_path: Path) -> None:
    first = controller.process_upload(make_upload(tmp_path, file_name="one.pdf"))
    second = controller.process_upload(make_upload(tmp_path, file_name="two.pdf"))

    result = controller.confirm_pending(12345)

    assert result.write_result.workbook_row_number == 4
    assert result.next_pending is second.pending
    assert controller.get_session(12345).pending is second.pending
    assert len(controller.get_session(12345).pending_queue) == 0


def test_cancel_advances_to_next_queued_receipt(controller, tmp_path: Path) -> None:
    first = controller.process_upload(make_upload(tmp_path, file_name="one.pdf"))
    second = controller.process_upload(make_upload(tmp_path, file_name="two.pdf"))

    transition = controller.cancel_pending(12345)

    assert transition.current is second.pending
    assert controller.get_session(12345).pending is second.pending
    assert len(controller.get_session(12345).pending_queue) == 0


def test_recent_transactions_are_scoped_to_the_requesting_user(controller, tmp_path: Path) -> None:
    controller.process_upload(make_upload(tmp_path, user_id=12345, file_name="one.pdf"))
    controller.confirm_pending(12345)
    controller.process_upload(make_upload(tmp_path, user_id=67890, file_name="two.pdf"))
    controller.confirm_pending(67890)

    user_one_rows = controller.list_recent_transactions(12345)
    user_two_rows = controller.list_recent_transactions(67890)

    assert len(user_one_rows) == 1
    assert len(user_two_rows) == 1
    assert user_one_rows[0]["id"] != user_two_rows[0]["id"]


def test_export_marks_the_current_users_audit_entry(controller, tmp_path: Path) -> None:
    controller.process_upload(make_upload(tmp_path, user_id=12345, file_name="one.pdf"))
    controller.confirm_pending(12345)
    controller.process_upload(make_upload(tmp_path, user_id=67890, file_name="two.pdf"))
    controller.confirm_pending(67890)

    controller.export_latest(12345)

    user_one = controller.audit_log.last_written_entry(12345)
    user_two = controller.audit_log.last_written_entry(67890)
    assert user_one is not None and user_one["exported"] == 1
    assert user_two is not None and user_two["exported"] == 0


def test_undo_candidate_is_scoped_to_the_requesting_user(controller, tmp_path: Path) -> None:
    controller.process_upload(make_upload(tmp_path, user_id=12345, file_name="one.pdf"))
    controller.confirm_pending(12345)
    controller.process_upload(make_upload(tmp_path, user_id=67890, file_name="two.pdf"))
    controller.confirm_pending(67890)

    candidate = controller.prepare_undo(12345)

    other_users_last_entry = controller.audit_log.last_written_entry(67890)
    assert other_users_last_entry is not None
    assert candidate["id"] != str(other_users_last_entry["id"])


def test_undo_requires_confirmation(controller, tmp_path: Path) -> None:
    controller.process_upload(make_upload(tmp_path))
    controller.confirm_pending(12345)
    candidate = controller.prepare_undo(12345)
    assert candidate["account"] == "Petrol"
    backup_path = controller.confirm_undo(12345)
    assert backup_path.exists()
