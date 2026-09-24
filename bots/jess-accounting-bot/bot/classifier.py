from __future__ import annotations

from decimal import Decimal

from .accounts import CONTRACTOR_RULES, KEYWORD_RULES, is_valid_account, resolve_account_alias
from .models import Direction, ProposedTransaction, ReceiptExtraction


class ReceiptClassifier:
    def classify(self, extraction: ReceiptExtraction) -> ProposedTransaction:
        account, reason, review = self._resolve_account(extraction)
        confidence = float(extraction.confidence)

        if extraction.currency != "ZAR":
            review = True
            reason = f"{reason}; non-ZAR receipt detected"

        if extraction.total_amount_zar is None:
            raise ValueError("No amount found in the receipt")
        if extraction.transaction_date is None:
            raise ValueError("No date found in the receipt")

        notes = extraction.notes.strip() or self._build_default_notes(extraction)
        confidence = min(confidence, 0.64) if review else max(confidence, 0.85)

        return ProposedTransaction(
            transaction_date=extraction.transaction_date,
            direction=extraction.direction,
            account=account,
            amount=extraction.total_amount_zar,
            notes=notes[:120],
            confidence=confidence,
            needs_user_review=review or extraction.needs_user_review or confidence < 0.75,
            reasoning_summary=reason,
            merchant_name=extraction.merchant_name,
            document_type=extraction.document_type,
        )

    def _resolve_account(self, extraction: ReceiptExtraction) -> tuple[str, str, bool]:
        if extraction.direction == Direction.INCOME:
            text = self._combined_text(extraction)
            if "ticket" in text or "showcase" in text:
                return "Ticket sales from Showcase", "Matched income keywords for showcase ticket sales", False
            return "Fees earned", "Defaulted income to fees earned", False

        if is_valid_account(extraction.account):
            return extraction.account or "Accounting Fees", "Model supplied a valid approved account", False
        aliased_account = resolve_account_alias(extraction.account)
        if aliased_account is not None:
            return aliased_account, f"Mapped OCR account alias '{extraction.account}' to approved account", True

        text = self._combined_text(extraction)
        for keyword, account in CONTRACTOR_RULES.items():
            if keyword in text:
                return account, f"Matched contractor keyword '{keyword}'", False

        matches = [
            rule.account
            for rule in KEYWORD_RULES
            if (
                all(keyword in text for keyword in rule.keywords)
                if rule.match_mode == "all"
                else any(keyword in text for keyword in rule.keywords)
            )
        ]
        unique_matches = list(dict.fromkeys(matches))
        if len(unique_matches) == 1:
            return unique_matches[0], f"Matched keyword rule for {unique_matches[0]}", False
        if len(unique_matches) > 1:
            return unique_matches[0], "Multiple possible account matches found", True
        return "Accounting Fees", "No confident account match; defaulted to review-required expense bucket", True

    def _combined_text(self, extraction: ReceiptExtraction) -> str:
        return " ".join(
            part.lower()
            for part in (
                extraction.merchant_name or "",
                extraction.line_items_summary,
                extraction.notes,
            )
            if part
        )

    def _build_default_notes(self, extraction: ReceiptExtraction) -> str:
        merchant = extraction.merchant_name or "Unknown merchant"
        summary = extraction.line_items_summary.strip()
        parts = [merchant]
        if summary:
            parts.append(summary)
        return " - ".join(parts)
