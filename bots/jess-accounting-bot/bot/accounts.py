from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

INCOME_ACCOUNTS = [
    "Fees earned",
    "Ticket sales from Showcase",
]

EXPENSE_ACCOUNTS = [
    "Accounting Fees",
    "Advertising",
    "Bank Charges",
    "Hall Hire Charterhouse",
    "Corporate Gifts",
    "Costumes and Props",
    "Theatre Hire",
    "Entertainment",
    "Roodepoort Dance Festival Entries",
    "Examination costs",
    "Hall Hire Church",
    "Flooring for Studio",
    "Internet",
    "Medals",
    "Motor Vehicle Expense",
    "Payment to contractor - Edmund",
    "Payment to contractor - Backstage Help at Showcase",
    "Payment to contractor - Videographer for Showcase",
    "Payment to contractor - Reese",
    "Payment to contractor - Jenna (Exam Music)",
    "Payment to contractor - Luisa",
    "Payment to contractor - Cristina",
    "Petrol",
    "Phone",
    "Printing",
    "Public Liability Insurance",
    "RAD/AIDT Membership Fee",
    "CPD Courses",
    "Sewing",
    "Stationery",
    "Storage for Costumes and Props",
    "Studio supplies - Apple Music",
    "Studio supplies - Hardware/Tape/Lights/Gas",
    "Studio supplies - Sweets/Gifts/Stickers",
    "Studio supplies - Shoe Dye/Stockings",
    "Studio supplies - Dressing room supplies",
    "Studio supplies - Teaching Shoes/Uniform",
    "Website",
    "Home Office Space Rent",
    "Google Storage",
    "Refunds",
    "Retirement Annuity",
]

APPROVED_ACCOUNTS = INCOME_ACCOUNTS + EXPENSE_ACCOUNTS
APPROVED_ACCOUNT_SET = set(APPROVED_ACCOUNTS)

@dataclass(frozen=True, slots=True)
class KeywordRule:
    account: str
    keywords: tuple[str, ...]
    match_mode: str = "any"


KEYWORD_RULES: list[KeywordRule] = [
    KeywordRule(account="Petrol", keywords=("shell", "engen", "bp", "sasol", "petrol")),
    KeywordRule(account="Motor Vehicle Expense", keywords=("uber", "bolt", "parking", "toll", "vehicle", "transport", "taxi", "shuttle")),
    KeywordRule(account="Phone", keywords=("vodacom", "mtn", "cell c", "telkom mobile")),
    KeywordRule(account="Internet", keywords=("fibre", "wifi", "wi-fi", "isp", "afrihost", "internet")),
    KeywordRule(account="Printing", keywords=("print", "printing", "flyer", "poster", "certificate")),
    KeywordRule(account="Google Storage", keywords=("google one", "google drive", "google workspace", "google storage")),
    KeywordRule(account="Website", keywords=("wix", "squarespace", "domain", "hosting", "website")),
    KeywordRule(account="Studio supplies - Apple Music", keywords=("apple music",)),
    KeywordRule(account="Studio supplies - Hardware/Tape/Lights/Gas", keywords=("hardware", "builders", "tape", "lights", "gas", "tool")),
    KeywordRule(account="Studio supplies - Sweets/Gifts/Stickers", keywords=("sweet", "gift", "sticker")),
    KeywordRule(account="Studio supplies - Shoe Dye/Stockings", keywords=("shoe dye", "stocking")),
    KeywordRule(account="Studio supplies - Dressing room supplies", keywords=("dressing room", "hanger", "mirror")),
    KeywordRule(account="Studio supplies - Teaching Shoes/Uniform", keywords=("teaching shoes", "teaching uniform")),
    KeywordRule(account="Sewing", keywords=("fabric", "alteration", "seamstress", "sewing")),
    KeywordRule(account="Costumes and Props", keywords=("costume", "prop", "stage item")),
    KeywordRule(account="Theatre Hire", keywords=("theatre",)),
    KeywordRule(account="Hall Hire Church", keywords=("church hall", "church")),
    KeywordRule(account="Hall Hire Charterhouse", keywords=("charterhouse",)),
    KeywordRule(account="RAD/AIDT Membership Fee", keywords=("rad", "aidt", "membership")),
    KeywordRule(account="Examination costs", keywords=("exam", "examination")),
    KeywordRule(account="Roodepoort Dance Festival Entries", keywords=("festival", "roodepoort"), match_mode="all"),
    KeywordRule(account="Public Liability Insurance", keywords=("public liability", "liability insurance")),
    KeywordRule(account="Accounting Fees", keywords=("accountant", "accounting", "bookkeeper", "tax practitioner")),
    KeywordRule(account="Advertising", keywords=("advert", "advertising", "marketing", "boosted post", "social media")),
    KeywordRule(account="Entertainment", keywords=("meal", "restaurant", "hospitality", "entertainment")),
    KeywordRule(account="Storage for Costumes and Props", keywords=("storage",)),
    KeywordRule(account="Retirement Annuity", keywords=("retirement annuity",)),
]

CONTRACTOR_RULES = {
    "edmund": "Payment to contractor - Edmund",
    "backstage": "Payment to contractor - Backstage Help at Showcase",
    "videographer": "Payment to contractor - Videographer for Showcase",
    "reese": "Payment to contractor - Reese",
    "jenna": "Payment to contractor - Jenna (Exam Music)",
    "luisa": "Payment to contractor - Luisa",
    "cristina": "Payment to contractor - Cristina",
}

ACCOUNT_ALIASES = {
    "fees earned": "Fees earned",
    "travel and transport": "Motor Vehicle Expense",
    "transport": "Motor Vehicle Expense",
    "transport expense": "Motor Vehicle Expense",
    "travel expense": "Motor Vehicle Expense",
    "fuel": "Petrol",
    "cellphone": "Phone",
    "mobile phone": "Phone",
    "internet expense": "Internet",
    "web hosting": "Website",
}


def is_valid_account(account: str | None) -> bool:
    return bool(account and account in APPROVED_ACCOUNT_SET)


def resolve_account_alias(account: str | None) -> str | None:
    if not account:
        return None
    return ACCOUNT_ALIASES.get(account.strip().lower())


def approved_accounts_text(accounts: Iterable[str] | None = None) -> str:
    values = list(accounts or APPROVED_ACCOUNTS)
    return "\n".join(f"- {value}" for value in values)
