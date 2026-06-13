"""Domain model and scoring rule for the AI-offence bill tracker.

A bill is flagged when its text mentions BOTH a criminal-offence term AND a term
suggesting the conduct could feasibly be carried out using AI. Every keyword occurrence
scores one point; flagged bills are ranked by total points (criminal + AI), highest first.

Pure scoring: no I/O and no XML parsing. serve.py supplies each bill's text; this module
decides whether, and why, it is flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- Keyword vocabularies ------------------------------------------------------------
# Criterion 1 — does the bill mention a criminal offence?
CRIMINAL_KEYWORDS: tuple[str, ...] = ("crime", "harm", "commit", "offence", "liable", "conviction")

# Criterion 2 — could the conduct feasibly be carried out using AI?
# NOTE: "artificial intelligence" overlaps with the standalone "artificial"; text containing
# the phrase therefore scores on both. Drop one entry if you want the phrase counted once.
AI_KEYWORDS: tuple[str, ...] = (
    "artificial intelligence",
    "online",
    "digital",
    "artificial",
    "computer",
    "neural",
    "technology",
    "network",
)


def _compile_patterns(keywords: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Compile each keyword to a case-insensitive, whole-word, plural-tolerant pattern.

    A trailing optional "s" lets plurals / 3rd-person forms count (offence/offences,
    commit/commits). Word boundaries stop false positives such as "committee" matching
    "commit" or "pharmacy" matching "harm". Other inflections (criminal, committed,
    convicted, harmful, liability) are deliberately NOT matched — broadening to prefixes
    would re-admit "committee", so recall here is traded for precision.

    Args:
        keywords: The keyword vocabulary to compile.

    Returns:
        Pairs of (keyword, compiled pattern), preserving input order.
    """
    return tuple((keyword, re.compile(rf"\b{re.escape(keyword)}s?\b", re.IGNORECASE)) for keyword in keywords)


CRIMINAL_PATTERNS = _compile_patterns(CRIMINAL_KEYWORDS)
AI_PATTERNS = _compile_patterns(AI_KEYWORDS)


@dataclass(frozen=True)
class KeywordHit:
    """One keyword that appeared in a bill, with its occurrence count.

    Attributes:
        keyword: The matched keyword from a vocabulary.
        count: Number of occurrences in the bill text (each scores one point).
    """

    keyword: str
    count: int


@dataclass
class ScoredBill:
    """A bill scored against the two keyword criteria.

    Attributes:
        bill_id: Parliament Bills API billId.
        bill_name: Bill short title.
        criminal_hits: Criminal-offence keywords found, with counts.
        ai_hits: AI-capability keywords found, with counts.
    """

    bill_id: int
    bill_name: str
    criminal_hits: list[KeywordHit]
    ai_hits: list[KeywordHit]

    @property
    def criminal_score(self) -> int:
        """Total criminal-keyword occurrences (one point each)."""
        return sum(hit.count for hit in self.criminal_hits)

    @property
    def ai_score(self) -> int:
        """Total AI-keyword occurrences (one point each)."""
        return sum(hit.count for hit in self.ai_hits)

    @property
    def total_score(self) -> int:
        """Combined score used to rank flagged bills, highest first."""
        return self.criminal_score + self.ai_score

    @property
    def is_flagged(self) -> bool:
        """Flagged only if BOTH criteria are met: a criminal term AND an AI term.

        Switch the ``and`` to ``or`` to flag on either signal instead.
        """
        return self.criminal_score > 0 and self.ai_score > 0

    @property
    def matched_keywords(self) -> list[str]:
        """Distinct keywords that fired, criminal terms first then AI."""
        return [hit.keyword for hit in self.criminal_hits] + [hit.keyword for hit in self.ai_hits]


def _count_hits(text: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> list[KeywordHit]:
    """Count occurrences of each keyword pattern in the text.

    Args:
        text: The bill text to scan.
        patterns: Compiled (keyword, pattern) pairs.

    Returns:
        A KeywordHit per keyword that occurred at least once, in vocabulary order.
    """
    hits: list[KeywordHit] = []
    for keyword, pattern in patterns:
        count = len(pattern.findall(text))
        if count:
            hits.append(KeywordHit(keyword=keyword, count=count))
    return hits


def score_bill(bill_id: int, bill_name: str, text: str) -> ScoredBill:
    """Score one bill's text against the criminal and AI keyword vocabularies.

    Args:
        bill_id: Parliament Bills API billId.
        bill_name: Bill short title.
        text: The bill's substantive text.

    Returns:
        A ScoredBill carrying the keyword hits and derived scores.
    """
    return ScoredBill(
        bill_id=bill_id,
        bill_name=bill_name,
        criminal_hits=_count_hits(text, CRIMINAL_PATTERNS),
        ai_hits=_count_hits(text, AI_PATTERNS),
    )