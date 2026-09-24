from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from bot.ocr_client import OpenAIReceiptOCRClient, parse_extraction_payload


def test_parse_valid_extraction() -> None:
    extraction = parse_extraction_payload(
        """
        {
          "document_type": "receipt",
          "merchant_name": "Shell",
          "transaction_date": "2026-06-10",
          "total_amount_zar": 642.30,
          "currency": "ZAR",
          "vat_amount": 83.78,
          "payment_method": "Card",
          "invoice_or_receipt_number": "12345",
          "line_items_summary": "Petrol",
          "direction": "Expense",
          "account": "Petrol",
          "notes": "Shell - petrol",
          "confidence": 0.92,
          "needs_user_review": false,
          "reasoning_summary": "Fuel slip"
        }
        """
    )
    assert extraction.merchant_name == "Shell"
    assert extraction.transaction_date.isoformat() == "2026-06-10"
    assert str(extraction.total_amount_zar) == "642.3"


def test_parse_missing_date() -> None:
    extraction = parse_extraction_payload(
        '{"document_type":"receipt","transaction_date":null,"total_amount_zar":10,"currency":"ZAR","direction":"Expense","line_items_summary":"","notes":"","confidence":0.4,"needs_user_review":true,"reasoning_summary":""}'
    )
    assert extraction.transaction_date is None


def test_parse_missing_amount() -> None:
    extraction = parse_extraction_payload(
        '{"document_type":"receipt","transaction_date":"2026-06-10","total_amount_zar":null,"currency":"ZAR","direction":"Expense","line_items_summary":"","notes":"","confidence":0.4,"needs_user_review":true,"reasoning_summary":""}'
    )
    assert extraction.total_amount_zar is None


def test_parse_non_zar_currency() -> None:
    extraction = parse_extraction_payload(
        '{"document_type":"receipt","transaction_date":"2026-06-10","total_amount_zar":10,"currency":"usd","direction":"Expense","line_items_summary":"","notes":"","confidence":0.4,"needs_user_review":true,"reasoning_summary":""}'
    )
    assert extraction.currency == "USD"


def test_parse_low_confidence() -> None:
    extraction = parse_extraction_payload(
        '{"document_type":"receipt","transaction_date":"2026-06-10","total_amount_zar":10,"currency":"ZAR","direction":"Expense","line_items_summary":"","notes":"","confidence":0.2,"needs_user_review":true,"reasoning_summary":""}'
    )
    assert extraction.confidence == 0.2
    assert extraction.needs_user_review is True


def test_parse_invalid_json() -> None:
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_extraction_payload("{not-json}")


def test_parse_extracts_json_from_markdown_wrapper() -> None:
    extraction = parse_extraction_payload(
        """Here is the extracted structured data:

```json
{"document_type":"receipt","merchant_name":"Shell","transaction_date":"2026-06-10","total_amount_zar":"642.30","currency":"ZAR","vat_amount":null,"payment_method":null,"invoice_or_receipt_number":null,"line_items_summary":"Petrol","direction":"Expense","account":"Petrol","notes":"Shell - petrol","confidence":0.95,"needs_user_review":false,"reasoning_summary":"Clear fuel slip"}
```"""
    )
    assert extraction.account == "Petrol"


def test_parse_valid_extraction_from_json_mode_payload() -> None:
    extraction = parse_extraction_payload(
        '{"document_type":"receipt","merchant_name":"Shell","transaction_date":"2026-06-10","total_amount_zar":"642.30","currency":"ZAR","vat_amount":null,"payment_method":null,"invoice_or_receipt_number":null,"line_items_summary":"Petrol","direction":"Expense","account":"Petrol","notes":"Shell - petrol","confidence":0.95,"needs_user_review":false,"reasoning_summary":"Clear fuel slip"}'
    )
    assert extraction.account == "Petrol"
    assert extraction.needs_user_review is False


def test_parse_normalizes_provider_casing_and_null_strings() -> None:
    extraction = parse_extraction_payload(
        '{"document_type":"Unknown","merchant_name":"Unknown merchant","transaction_date":null,"total_amount_zar":null,"currency":"zar","vat_amount":null,"payment_method":null,"invoice_or_receipt_number":null,"line_items_summary":null,"direction":"expense","account":null,"notes":null,"confidence":0.21,"needs_user_review":true,"reasoning_summary":null}'
    )
    assert extraction.document_type == "unknown"
    assert extraction.currency == "ZAR"
    assert extraction.line_items_summary == ""
    assert extraction.notes == ""


def write_sample_pdf(path: Path) -> None:
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 240] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        "<< /Length 44 >>\nstream\nBT /F1 18 Tf 20 120 Td (Hello PDF) Tj ET\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    parts = [b"%PDF-1.4\n"]
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(part) for part in parts))
        parts.append(f"{index} 0 obj\n{obj}\nendobj\n".encode("utf-8"))
    xref_offset = sum(len(part) for part in parts)
    parts.append(f"xref\n0 {len(objects) + 1}\n".encode("utf-8"))
    parts.append(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        parts.append(f"{offset:010d} 00000 n \n".encode("utf-8"))
    parts.append(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("utf-8")
    )
    path.write_bytes(b"".join(parts))


def test_render_pdf_to_png_data_urls(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    write_sample_pdf(pdf_path)
    client = OpenAIReceiptOCRClient(api_key="test-key", model="test-model", base_url="https://example.com/v1")
    data_urls = client._render_pdf_to_png_data_urls(pdf_path)
    assert len(data_urls) == 1
    assert data_urls[0].startswith("data:image/png;base64,")
    assert len(base64.b64decode(data_urls[0].split(",", 1)[1])) > 50


def test_compatible_provider_pdf_extract_uses_rasterized_pages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    write_sample_pdf(pdf_path)
    client = OpenAIReceiptOCRClient(api_key="test-key", model="test-model", base_url="https://example.com/v1")

    monkeypatch.setattr(client, "_render_pdf_to_png_data_urls", lambda path: ["data:image/png;base64,abc123"])

    captured: dict[str, object] = {}

    class FakeMessage:
        content = json.dumps(
            {
                "document_type": "receipt",
                "merchant_name": "Shell",
                "transaction_date": "2026-06-10",
                "total_amount_zar": "642.30",
                "currency": "ZAR",
                "vat_amount": None,
                "payment_method": None,
                "invoice_or_receipt_number": None,
                "line_items_summary": "Petrol",
                "direction": "Expense",
                "account": "Petrol",
                "notes": "Shell - petrol",
                "confidence": 0.93,
                "needs_user_review": False,
                "reasoning_summary": "Clear receipt",
            }
        )

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    def fake_create(**kwargs):
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(client.client.chat.completions, "create", fake_create)

    extraction = client.extract(pdf_path, "application/pdf")

    assert extraction.account == "Petrol"
    content = captured["messages"][0]["content"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,abc123"


def test_openai_path_falls_back_to_chat_completions_when_parse_validation_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    image_path = tmp_path / "sample.jpg"
    image_path.write_bytes(b"fake-image")
    client = OpenAIReceiptOCRClient(api_key="test-key", model="test-model")

    def fake_parse(**kwargs):
        raise ValueError("bad structured parse")

    class FakeMessage:
        content = json.dumps(
            {
                "document_type": "receipt",
                "merchant_name": "Shell",
                "transaction_date": "2026-06-10",
                "total_amount_zar": "642.30",
                "currency": "ZAR",
                "vat_amount": None,
                "payment_method": None,
                "invoice_or_receipt_number": None,
                "line_items_summary": "Petrol",
                "direction": "Expense",
                "account": "Petrol",
                "notes": "Shell - petrol",
                "confidence": 0.93,
                "needs_user_review": False,
                "reasoning_summary": "Clear receipt",
            }
        )

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    monkeypatch.setattr(client.client.responses, "parse", fake_parse)
    monkeypatch.setattr(client.client.chat.completions, "create", lambda **kwargs: FakeResponse())

    extraction = client.extract(image_path, "image/jpeg")
    assert extraction.account == "Petrol"
