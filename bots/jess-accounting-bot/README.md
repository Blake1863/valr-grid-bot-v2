# Jess Lombard Junior Dance Centre Receipt Bot

Telegram receipt bot that accepts receipt images or PDFs, extracts accounting details with OpenAI vision, asks for confirmation or edits, writes confirmed transactions into the existing Excel workbook, and can export the updated `.xlsx` file back to the authorized Telegram user.

## What It Does

- Accepts Telegram photos and supported documents: PDF, JPG, JPEG, PNG, HEIC/HEIF
- Restricts usage to authorized Telegram user IDs
- Uses OpenAI Responses API vision/file input for OCR and structured extraction
- Applies deterministic account classification safeguards against the approved account list
- Requires explicit confirmation before any workbook write
- Creates timestamped backups before writes
- Supports dry-run preview workbooks and batch mode
- Queues multiple uploaded receipts per user and automatically advances to the next one after confirm/cancel
- Stores an external SQLite audit log
- Offers `/last`, `/undo`, `/backup`, and `/export`

## Project Layout

```text
bot/
  main.py
  config.py
  telegram_handlers.py
  controller.py
  ocr_client.py
  classifier.py
  workbook_writer.py
  audit_log.py
  models.py
  accounts.py
tests/
```

## Setup

1. Create a virtualenv and install dependencies:

```bash
cd /home/admin/.openclaw/workspace/bots/jess-accounting-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
```

2. Copy `.env.example` to `.env` and fill in the real values.

   `OPENAI_BASE_URL` is optional and only needed if you want to use an OpenAI-compatible API endpoint instead of the default OpenAI API.

3. Point `WORKBOOK_PATH` at the real `2026 Jess Lombard Junior Dance Centre Accounting.xlsx`.

4. Start the bot:

```bash
. .venv/bin/activate
python -m bot.main
```

## Telegram Commands

- `/start` explains usage
- `/accounts` shows the approved account list
- `/last` shows the last 5 bot-added transactions
- `/undo` asks to undo the most recent bot-written transaction
- `/export` exports the latest workbook copy
- `/backup` creates a backup copy
- `/dryrun_on` and `/dryrun_off` toggle dry-run mode
- `/batch_on` and `/batch_off` toggle batch mode

## How OCR Works

`bot/ocr_client.py` supports two OCR paths:

- Default OpenAI path:
  - Images are sent as `input_image`
  - PDFs are uploaded with the Files API and passed as `input_file`
  - The model is forced into a structured schema matching `ReceiptExtraction`
- OpenAI-compatible path, such as DashScope / Model Studio:
  - Images are sent through chat completions in JSON mode
  - PDFs are converted locally into PNG page images before OCR
  - Parsed JSON is validated against the same `ReceiptExtraction` schema

The extracted JSON includes the concise `reasoning_summary` but does not store chain-of-thought.

## How Classification Works

`bot/classifier.py` takes the structured extraction and:

- Accepts an exact approved account only when it already matches the whitelist
- Uses deterministic keyword rules for common merchants and categories
- Forces review on ambiguous matches, non-ZAR receipts, or weak confidence
- Defaults income to `Fees earned` unless ticket/showcase language clearly points to `Ticket sales from Showcase`

## How Workbook Writing Works

`bot/workbook_writer.py`:

- Opens the existing workbook instead of recreating it
- Finds the last transaction row by checking populated values in columns `C:G`
- Appends into the next logical row
- Writes the date as a real Excel date and amount as numeric
- Copies row styling from the previous transaction row
- Expands fixed formula ranges that point at `Transactions Raw`
- Validates headers, sheets, and formula presence after writing

## Export, Backups, and Undo

- Every live write creates `BACKUP_<prefix>_YYYY-MM-DD_HHMMSS.xlsx`
- Dry-run creates `PREVIEW_<prefix>_YYYY-MM-DD_HHMM.xlsx`
- `/export` sends a copied workbook named `<prefix>_updated_YYYY-MM-DD_HHMM.xlsx`
- Audit entries are stored in SQLite outside the workbook
- `/undo` only targets the most recent bot-written row, verifies it still matches the audit log, then clears that last row safely

## Tests

Run the test suite with:

```bash
. .venv/bin/activate
pytest
```

The tests cover:

- OCR JSON parsing
- Account classification rules
- Workbook append, formula extension, backups, preview safety, and undo
- Controller flows for authorization, upload, confirm, edit, cancel, export, dry-run, batch mode, and undo

## Current Limits

- HEIC handling depends on Telegram delivering a supported image file and OpenAI accepting it directly. If Telegram or the upstream model rejects a specific HEIC file, the bot should ask for JPG, PNG, or PDF instead.
