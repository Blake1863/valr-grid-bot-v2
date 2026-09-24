from __future__ import annotations

import logging

from .audit_log import AuditLog
from .classifier import ReceiptClassifier
from .config import load_settings
from .controller import ReceiptBotController
from .ocr_client import OpenAIReceiptOCRClient
from .telegram_handlers import build_application
from .workbook_writer import WorkbookWriter


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    settings = load_settings()

    controller = ReceiptBotController(
        ocr_client=OpenAIReceiptOCRClient(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.openai_base_url,
        ),
        classifier=ReceiptClassifier(),
        workbook_writer=WorkbookWriter(
            workbook_path=settings.workbook_path,
            output_dir=settings.workbook_output_dir,
            backup_dir=settings.workbook_backup_dir,
            backup_retention_count=settings.backup_retention_count,
            export_filename_prefix=settings.export_filename_prefix,
        ),
        audit_log=AuditLog(settings.sqlite_path),
        authorized_user_ids=set(settings.authorized_telegram_user_ids),
        default_dry_run=settings.default_dry_run_mode,
        default_batch_mode=settings.default_batch_mode,
        rate_limit_window_seconds=settings.rate_limit_window_seconds,
        rate_limit_max_uploads=settings.rate_limit_max_uploads,
    )
    application = build_application(
        token=settings.telegram_bot_token,
        controller=controller,
        uploads_dir=settings.uploads_dir,
        max_upload_bytes=settings.max_upload_bytes,
    )
    application.run_polling()


if __name__ == "__main__":
    main()
