"""Synthetic member data.

Every value here is fabricated. No real PII, no real account numbers. The
member IDs in the 5xxxx/9xxxx range are reserved as error triggers so the
replay error-taxonomy suite has deterministic conditions to fire.
"""

from dataclasses import dataclass, field


@dataclass
class Account:
    number: str
    kind: str
    balance: float
    status: str = "Active"


@dataclass
class Member:
    number: str
    name: str
    ssn_last4: str
    branch: str
    since: str
    accounts: list[Account] = field(default_factory=list)


# Deliberate error-trigger IDs, documented so replays can exercise each path.
MEMBER_NOT_FOUND = "99999"
MEMBER_PERMISSION_DENIED = "55555"
DEPOSIT_LIMIT = 10_000.00

MEMBERS: dict[str, Member] = {
    "12345": Member(
        number="12345",
        name="Dana Whitfield",
        ssn_last4="4417",
        branch="Cedar Falls Main",
        since="2011-03-14",
        accounts=[
            Account("0001234501", "Regular Savings", 8_412.55),
            Account("0001234502", "Free Checking", 1_902.18),
            Account("0001234503", "Holiday Club", 350.00),
        ],
    ),
    "23456": Member(
        number="23456",
        name="Marcus Oyelaran",
        ssn_last4="9930",
        branch="Riverbend",
        since="2018-08-02",
        accounts=[
            Account("0002345601", "Regular Savings", 214.09),
            Account("0002345602", "Free Checking", 76.44, status="Restricted"),
        ],
    ),
    "34567": Member(
        number="34567",
        name="Priya Raghunathan",
        ssn_last4="1288",
        branch="Cedar Falls Main",
        since="2005-11-30",
        accounts=[
            Account("0003456701", "Regular Savings", 41_230.71),
            Account("0003456702", "Money Market", 125_004.02),
        ],
    ),
    MEMBER_PERMISSION_DENIED: Member(
        number=MEMBER_PERMISSION_DENIED,
        name="RESTRICTED RECORD",
        ssn_last4="0000",
        branch="Executive",
        since="1999-01-01",
        accounts=[],
    ),
}

ACCOUNT_TYPES = [
    "Regular Savings",
    "Holiday Club",
    "Vacation Club",
    "Youth Savings",
]

FUNDING_SOURCES = ["Free Checking", "Regular Savings", "External Transfer"]

_next_account_seq = 9000


def next_account_number(member_no: str) -> str:
    global _next_account_seq
    _next_account_seq += 1
    return f"{member_no.zfill(5)}{_next_account_seq}"


def reset() -> None:
    """Restore mutable state. Used by the test suite between cases."""
    global _next_account_seq
    _next_account_seq = 9000
    for m in MEMBERS.values():
        m.accounts = [a for a in m.accounts if not a.kind.endswith("(new)")]
