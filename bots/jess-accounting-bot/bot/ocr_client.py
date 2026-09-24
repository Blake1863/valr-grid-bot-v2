from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Any

from openai import OpenAI
from pydantic import ValidationError

from .models import ReceiptExtraction

OCR_PROMPT = """
You extract structured accounting information from South African receipts, invoices,
proofs of payment, and income receipts for a dance centre.

Return only the schema fields. Do not invent missing values.
- Currency should be ZAR when the receipt clearly uses rand.
- Direction must be Expense or Income.
- Account must be one exact approved account name when you are confident, otherwise leave it null.
- Notes should be concise and spreadsheet-friendly.
- confidence must be between 0 and 1.
- needs_user_review should be true unless the receipt is very clear.
- reasoning_summary should be brief and non-sensitive.
""".strip()

JSON_SCHEMA_HINT = """
Return JSON only with these keys:
document_type, merchant_name, transaction_date, total_amount_zar, currency,
vat_amount, payment_method, invoice_or_receipt_number, line_items_summary,
direction, account, notes, confidence, needs_user_review, reasoning_summary.
""".strip()

MAX_COMPAT_PDF_PAGES = 3


def parse_extraction_payload(payload: str) -> ReceiptExtraction:
    cleaned = _extract_json_object(payload)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError("Model returned invalid JSON") from exc
    return ReceiptExtraction.model_validate(data)


def _extract_json_object(payload: str) -> str:
    stripped = payload.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped

    fenced = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", stripped, re.IGNORECASE)
    if fenced:
        return fenced.group(1)

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


class OpenAIReceiptOCRClient:
    def __init__(self, *, api_key: str, model: str, base_url: str | None = None) -> None:
        client_kwargs: dict[str, Any] = {"api_key": api_key, "base_url": base_url or "https://api.openai.com/v1"}
        self.client = OpenAI(**client_kwargs)
        self.model = model
        self.base_url = base_url

    def extract(self, file_path: Path, mime_type: str) -> ReceiptExtraction:
        if self.base_url:
            return self._extract_via_chat_completions(file_path, mime_type)

        try:
            return self._extract_via_responses_parse(file_path, mime_type)
        except (ValidationError, ValueError, TypeError):
            return self._extract_via_chat_completions(file_path, mime_type)

    def _extract_via_responses_parse(self, file_path: Path, mime_type: str) -> ReceiptExtraction:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": OCR_PROMPT}]
        if mime_type == "application/pdf":
            with file_path.open("rb") as handle:
                uploaded = self.client.files.create(file=handle, purpose="user_data")
            content.append({"type": "input_file", "file_id": uploaded.id})
        else:
            encoded = base64.b64encode(file_path.read_bytes()).decode("utf-8")
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{mime_type};base64,{encoded}",
                }
            )

        response = self.client.responses.parse(
            model=self.model,
            input=[{"role": "user", "content": content}],
            text_format=ReceiptExtraction,
        )
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise ValueError("OCR model did not return a structured response")
        return parsed

    def _extract_via_chat_completions(self, file_path: Path, mime_type: str) -> ReceiptExtraction:
        content: list[dict[str, Any]] = [{"type": "text", "text": f"{OCR_PROMPT}\n\n{JSON_SCHEMA_HINT}"}]
        if mime_type == "application/pdf":
            for data_url in self._render_pdf_to_png_data_urls(file_path):
                content.append({"type": "image_url", "image_url": {"url": data_url}})
        else:
            encoded = base64.b64encode(file_path.read_bytes()).decode("utf-8")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                }
            )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": content,
                }
            ],
            response_format={"type": "json_object"},
        )
        payload = response.choices[0].message.content
        if not payload:
            raise ValueError("OCR model did not return any content")
        return parse_extraction_payload(payload)

    def _render_pdf_to_png_data_urls(self, file_path: Path, *, max_pages: int = MAX_COMPAT_PDF_PAGES) -> list[str]:
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise ValueError("PDF OCR requires pypdfium2 to be installed.") from exc

        document = pdfium.PdfDocument(str(file_path))
        page_total = len(document)
        if page_total <= 0:
            raise ValueError("The PDF has no pages to scan.")

        data_urls: list[str] = []
        for page_index in range(min(page_total, max_pages)):
            page = document[page_index]
            bitmap = page.render(scale=2.0)
            image = bitmap.to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
            data_urls.append(f"data:image/png;base64,{encoded}")
        return data_urls
