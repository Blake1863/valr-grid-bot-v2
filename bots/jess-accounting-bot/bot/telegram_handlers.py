from __future__ import annotations

import logging
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .accounts import approved_accounts_text
from .controller import ReceiptBotController
from .file_utils import build_upload_path, sniff_mime_type, validate_supported_file
from .models import UploadedReceipt

EDITABLE_FIELDS = {
    "date": "transaction_date",
    "amount": "amount",
    "account": "account",
    "notes": "notes",
}

logger = logging.getLogger(__name__)


def preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Confirm", callback_data="tx:confirm"),
                InlineKeyboardButton("Cancel", callback_data="tx:cancel"),
            ],
            [
                InlineKeyboardButton("Edit Date", callback_data="tx:edit:date"),
                InlineKeyboardButton("Edit Amount", callback_data="tx:edit:amount"),
            ],
            [
                InlineKeyboardButton("Edit Account", callback_data="tx:edit:account"),
                InlineKeyboardButton("Edit Notes", callback_data="tx:edit:notes"),
            ],
        ]
    )


def post_write_keyboard(preview: bool) -> InlineKeyboardMarkup:
    export_label = "Send preview spreadsheet" if preview else "Send updated spreadsheet"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(export_label, callback_data="tx:send_export")],
            [InlineKeyboardButton("Add another receipt", callback_data="tx:add_another")],
            [InlineKeyboardButton("Finish and export spreadsheet", callback_data="tx:finish_export")],
        ]
    )


def undo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Confirm undo", callback_data="undo:confirm")],
            [InlineKeyboardButton("Cancel", callback_data="undo:cancel")],
        ]
    )


def build_application(*, token: str, controller: ReceiptBotController, uploads_dir: Path, max_upload_bytes: int) -> Application:
    application = ApplicationBuilder().token(token).build()
    application.bot_data["controller"] = controller
    application.bot_data["uploads_dir"] = uploads_dir
    application.bot_data["max_upload_bytes"] = max_upload_bytes

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("accounts", accounts_command))
    application.add_handler(CommandHandler("last", last_command))
    application.add_handler(CommandHandler("undo", undo_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("backup", backup_command))
    application.add_handler(CommandHandler("dryrun_on", dryrun_on_command))
    application.add_handler(CommandHandler("dryrun_off", dryrun_off_command))
    application.add_handler(CommandHandler("batch_on", batch_on_command))
    application.add_handler(CommandHandler("batch_off", batch_off_command))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, receipt_upload_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, edit_text_handler))
    application.add_error_handler(error_handler)
    return application


def get_controller(context: ContextTypes.DEFAULT_TYPE) -> ReceiptBotController:
    return context.application.bot_data["controller"]


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_authorized(update, context)
    if update.message is None:
        return
    text = (
        "Send a receipt photo, image, or PDF.\n"
        "I will extract the transaction, suggest a category, and ask you to confirm before anything is written.\n\n"
        "Commands:\n"
        "/accounts\n"
        "/last\n"
        "/undo\n"
        "/export\n"
        "/backup\n"
        "/dryrun_on /dryrun_off\n"
        "/batch_on /batch_off"
    )
    await update.message.reply_text(text)


async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_authorized(update, context)
    if update.message is not None:
        await update.message.reply_text(approved_accounts_text())


async def last_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_authorized(update, context)
    if update.message is None or update.effective_user is None:
        return
    rows = get_controller(context).list_recent_transactions(update.effective_user.id, 5)
    if not rows:
        await update.message.reply_text("No bot-written transactions yet.")
        return
    lines = []
    for row in rows:
        lines.append(
            f"{row['date']} | {row['direction']} | {row['account']} | R{row['amount']} | {row['notes']} | {row['status']}"
        )
    await update.message.reply_text("\n".join(lines))


async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_authorized(update, context)
    if update.message is None:
        return
    candidate = get_controller(context).prepare_undo(update.effective_user.id)
    await update.message.reply_text(
        "\n".join(
            [
                "Undo the most recent bot transaction?",
                f"Date: {candidate['date']}",
                f"Direction: {candidate['direction']}",
                f"Account: {candidate['account']}",
                f"Amount: R{candidate['amount']}",
                f"Notes: {candidate['notes']}",
            ]
        ),
        reply_markup=undo_keyboard(),
    )


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_authorized(update, context)
    export_path = get_controller(context).export_latest(update.effective_user.id)
    if update.message is not None:
        await update.message.reply_document(document=export_path.open("rb"), filename=export_path.name)


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_authorized(update, context)
    backup_path = get_controller(context).create_backup(update.effective_user.id)
    if update.message is not None:
        await update.message.reply_text(f"Backup created: {backup_path.name}")


async def dryrun_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_authorized(update, context)
    get_controller(context).set_dry_run(update.effective_user.id, True)
    if update.message is not None:
        await update.message.reply_text("Dry-run mode is on.")


async def dryrun_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_authorized(update, context)
    get_controller(context).set_dry_run(update.effective_user.id, False)
    if update.message is not None:
        await update.message.reply_text("Dry-run mode is off.")


async def batch_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_authorized(update, context)
    get_controller(context).set_batch_mode(update.effective_user.id, True)
    if update.message is not None:
        await update.message.reply_text("Batch mode is on.")


async def batch_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_authorized(update, context)
    get_controller(context).set_batch_mode(update.effective_user.id, False)
    if update.message is not None:
        await update.message.reply_text("Batch mode is off.")


async def receipt_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_authorized(update, context)
    if update.message is None or update.effective_user is None:
        return

    uploads_dir: Path = context.application.bot_data["uploads_dir"]
    max_upload_bytes: int = context.application.bot_data["max_upload_bytes"]

    if update.message.photo:
        photo = update.message.photo[-1]
        file_name = f"{photo.file_unique_id}.jpg"
        mime_type = "image/jpeg"
        telegram_file_id = photo.file_id
        file_size = photo.file_size or 0
    elif update.message.document:
        document = update.message.document
        file_name = document.file_name or f"{document.file_unique_id}.bin"
        mime_type = sniff_mime_type(file_name, document.mime_type)
        telegram_file_id = document.file_id
        file_size = document.file_size or 0
    else:
        await update.message.reply_text("Send a photo or supported document.")
        return

    logger.info(
        "receipt upload received user_id=%s message_id=%s file_name=%s mime_type=%s size=%s",
        update.effective_user.id,
        update.message.message_id,
        file_name,
        mime_type,
        file_size,
    )
    validate_supported_file(file_name, mime_type, max_upload_bytes, file_size)
    path = build_upload_path(uploads_dir, file_name)
    try:
        telegram_file = await context.bot.get_file(telegram_file_id)
        await telegram_file.download_to_drive(custom_path=str(path))
    except TelegramError as exc:
        logger.exception(
            "telegram image download failed user_id=%s message_id=%s file_id=%s file_name=%s",
            update.effective_user.id,
            update.message.message_id,
            telegram_file_id,
            file_name,
        )
        raise ValueError("I couldn't download that image from Telegram. Please send it again as a photo or JPG/PNG/PDF.") from exc

    upload = UploadedReceipt(
        telegram_user_id=update.effective_user.id,
        file_name=file_name,
        file_path=path,
        mime_type=mime_type,
        telegram_file_id=telegram_file_id,
        file_size=file_size,
    )
    upload_result = get_controller(context).process_upload(upload)
    pending = upload_result.pending
    logger.info(
        "receipt processed user_id=%s message_id=%s merchant=%s account=%s review=%s queued=%s queue_position=%s",
        update.effective_user.id,
        update.message.message_id,
        pending.extraction.merchant_name,
        pending.proposed.account,
        pending.proposed.needs_user_review,
        upload_result.queued,
        upload_result.queue_position,
    )
    if upload_result.queued:
        await update.message.reply_text(
            (
                f"Queued receipt {upload_result.queue_position}.\n"
                "I will show it after you confirm or cancel the current receipt."
            )
        )
        return

    await update.message.reply_text(pending.proposed.preview_text(), reply_markup=preview_keyboard())


async def edit_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    await ensure_authorized(update, context)
    pending = get_controller(context).apply_edit(update.effective_user.id, update.message.text)
    await update.message.reply_text(pending.proposed.preview_text(), reply_markup=preview_keyboard())


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or update.effective_user is None:
        return
    await ensure_authorized(update, context)
    await query.answer()

    if query.data == "tx:confirm":
        result = get_controller(context).confirm_pending(update.effective_user.id)
        status_message = f"Transaction {'previewed' if result.write_result.preview else 'recorded'} on row {result.write_result.workbook_row_number}."
        if result.next_pending is None:
            await query.message.reply_text(
                status_message,
                reply_markup=post_write_keyboard(result.write_result.preview),
            )
            return

        await query.message.reply_text(
            f"{status_message}\nNext queued receipt ({result.queue_length + 1} remaining including this one):"
        )
        await query.message.reply_text(result.next_pending.proposed.preview_text(), reply_markup=preview_keyboard())
        return

    if query.data == "tx:cancel":
        transition = get_controller(context).cancel_pending(update.effective_user.id)
        if transition.current is None:
            await query.message.reply_text("Cancelled. Nothing was written.")
            return

        await query.message.reply_text(
            f"Cancelled. Showing next queued receipt ({transition.queue_length + 1} remaining including this one)."
        )
        await query.message.reply_text(transition.current.proposed.preview_text(), reply_markup=preview_keyboard())
        return

    if query.data and query.data.startswith("tx:edit:"):
        field_key = query.data.split(":")[-1]
        field_name = EDITABLE_FIELDS[field_key]
        get_controller(context).begin_edit(update.effective_user.id, field_name)
        prompts = {
            "transaction_date": "Send the new date as YYYY-MM-DD.",
            "amount": "Send the new amount in rand, for example 642.30.",
            "account": "Send the exact approved account name.",
            "notes": "Send the new notes text.",
        }
        await query.message.reply_text(prompts[field_name])
        return

    if query.data in {"tx:send_export", "tx:finish_export"}:
        export_path = get_controller(context).export_latest(update.effective_user.id, prefer_preview=True)
        await query.message.reply_document(document=export_path.open("rb"), filename=export_path.name)
        if query.data == "tx:finish_export":
            await query.message.reply_text("Batch finished.")
        return

    if query.data == "tx:add_another":
        await query.message.reply_text("Send the next receipt.")
        return

    if query.data == "undo:confirm":
        backup_path = get_controller(context).confirm_undo(update.effective_user.id)
        await query.message.reply_text(f"Last transaction undone. Backup created: {backup_path.name}")
        return

    if query.data == "undo:cancel":
        await query.message.reply_text("Undo cancelled.")


async def ensure_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    controller = get_controller(context)
    user = update.effective_user
    if user is None:
        raise PermissionError("Missing Telegram user")
    controller.ensure_authorized(user.id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("unhandled Jess bot error", exc_info=context.error)
    message = "Something went wrong while processing that receipt."
    if context.error is not None:
        text = str(context.error)
        if text:
            message = f"{message}\n{text}"
    if isinstance(update, Update) and update.effective_message is not None:
        await update.effective_message.reply_text(message)
