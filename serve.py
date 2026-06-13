"""FastAPI service exposing the s.216A carrier-detection shortlist.

Wraps the offence-creation + Ofcom-nexus detection (currently living in smoke_test)
behind an HTTP API with auto-generated Swagger UI at ``/docs``. The cache of AKN bills is
scanned and scored once at startup and the flagged shortlist is held in app state (a single
shared instance); restart to re-scan after refreshing the cache with fetch_bills.py.

NOTE — new dependencies: this is the one module that takes on third-party libraries
(``fastapi``, ``uvicorn``). They are required for the Swagger UI; the standard library
cannot generate an OpenAPI schema. Install with: ``pip install fastapi uvicorn``.

Detection logic is imported from smoke_test to avoid duplication. The clean end state
is to promote ``extract_bill`` and friends into a ``detection.py`` module that both this
service and the smoke test import; this import is the interim seam, not the final boundary.

Run:  python serve.py        (or)   uvicorn serve:app --reload
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query, Request
from pydantic import BaseModel, Field

from models import CandidateProvision, OfcomNexusSignal, ScoringWeights, TrackedBill
from smoke_test import NON_BILL_FILENAMES, extract_bill

logger = logging.getLogger(__name__)

# --- Configuration -------------------------------------------------------------------
# Cache dir holding {billId}.xml AKN files (written by fetch_bills.py). Overridable via an
# env var so the service can point at a real cache without editing code.
DEFAULT_CACHE_DIR = Path("data/raw_xml")
CACHE_DIR = Path(os.environ.get("S216_CACHE_DIR", str(DEFAULT_CACHE_DIR)))

# Single shared scoring config; detection reads the triage weights from this one instance.
WEIGHTS = ScoringWeights()

# --- API metadata (surfaced in Swagger UI) -------------------------------------------
API_TITLE = "UK AI Bill Tracker — s.216A Carrier API"
API_VERSION = "0.1.0"
API_DESCRIPTION = (
    "Returns UK Parliament bills whose clauses carry machine-detected signals of being a "
    "potential carrier for Online Safety Act s.216A powers — an offence-creating clause, or "
    "an Ofcom/OSA nexus. Each result gives the bill number and the reason it was flagged. "
    "These signals are high-precision but are not a legal judgement: the analytical "
    "reachability filters remain a deliberate human-review step."
)

# --- uvicorn run defaults (used only by the __main__ convenience runner) -------------
SERVE_HOST = "127.0.0.1"
SERVE_PORT = 8000

# Human-readable labels for the auto-detected Ofcom-nexus signals (used to build `reason`).
NEXUS_LABELS: dict[OfcomNexusSignal, str] = {
    OfcomNexusSignal.AMENDS_ONLINE_SAFETY_ACT: "amends the Online Safety Act 2023",
    OfcomNexusSignal.AMENDS_COMMUNICATIONS_ACT: "amends the Communications Act 2003",
    OfcomNexusSignal.MENTIONS_OFCOM: "mentions Ofcom",
    OfcomNexusSignal.MENTIONS_PRIORITY_OFFENCE: "mentions a priority offence",
    OfcomNexusSignal.MENTIONS_SCHEDULE_7: "references Schedule 7",
}


# --- Response schema (documented in Swagger) -----------------------------------------


class FlaggedProvision(BaseModel):
    """One clause that triggered a detection signal."""

    clause_num: str = Field(..., description="AKN <num> of the clause, e.g. '1'.")
    heading: str = Field(..., description="AKN <heading> of the clause.")
    creates_offence: bool = Field(..., description="An offence-creation pattern fired on this clause.")
    ofcom_nexus: list[str] = Field(default_factory=list, description="OSA/Ofcom nexus signals that fired.")
    matched_terms: list[str] = Field(default_factory=list, description="Literal terms/patterns that matched.")
    triage_score: float = Field(..., description="Machine triage score; orders the review queue.")


class FlaggedBill(BaseModel):
    """A bill with at least one flagged clause, and the reason it was flagged."""

    bill_id: int = Field(..., description="Parliament Bills API billId (the cache filename stem).")
    short_title: str = Field(..., description="Bill short title.")
    carrier_family: str = Field(..., description="Coarse carrier-family bucket from the title.")
    reason: str = Field(..., description="Human-readable explanation of why the bill was flagged.")
    max_triage_score: float = Field(..., description="Highest provision triage score in the bill.")
    flagged_provisions: list[FlaggedProvision] = Field(default_factory=list)


class FlaggedBillsResponse(BaseModel):
    """Envelope for the flagged-bills query."""

    scanned: int = Field(..., description="Number of bills scanned in the cache.")
    count: int = Field(..., description="Number of flagged bills returned after filtering.")
    bills: list[FlaggedBill] = Field(default_factory=list)


# --- Detection loading + response transforms -----------------------------------------


def load_tracked_bills(cache_dir: Path, weights: ScoringWeights) -> list[TrackedBill]:
    """Parse and score every AKN file in the cache directory.

    Args:
        cache_dir: Directory of {billId}.xml files.
        weights: Scoring weights shared across the run.

    Returns:
        One TrackedBill per parseable file; unparseable files are skipped by extract_bill.
    """
    bills: list[TrackedBill] = []
    for path in sorted(cache_dir.glob("*.xml")):
        if path.name in NON_BILL_FILENAMES:
            continue
        bill = extract_bill(path, weights)
        if bill is not None:
            bills.append(bill)
    return bills


def nexus_phrase(provision: CandidateProvision) -> str:
    """Readable join of a provision's nexus signals, with the Schedule-7 caveat appended.

    Args:
        provision: A provision whose ofcom_nexus is non-empty.

    Returns:
        A comma-joined phrase; a lone Schedule-7 hit gets a verify-the-source note.
    """
    labels = [NEXUS_LABELS.get(signal, signal.value) for signal in sorted(provision.ofcom_nexus, key=lambda s: s.value)]
    phrase = ", ".join(labels)
    if provision.ofcom_nexus == frozenset({OfcomNexusSignal.MENTIONS_SCHEDULE_7}):
        phrase += " (verify this is the Online Safety Act's Schedule 7, not the bill's own)"
    return phrase


def build_reason(bill: TrackedBill) -> str:
    """Compose a human-readable reason a bill was flagged, from its machine signals.

    Args:
        bill: A bill with at least one flagged provision.

    Returns:
        A sentence naming the offence-creating and/or Ofcom-nexus clauses.
    """
    clauses_offence = [p for p in bill.provisions if p.creates_or_amends_offence]
    clauses_nexus = [p for p in bill.provisions if p.ofcom_nexus]
    parts: list[str] = []
    if clauses_offence:
        named = "; ".join(f"s.{p.clause_num} ({p.heading})" for p in clauses_offence)
        parts.append(f"creates or amends a criminal offence at {named}")
    if clauses_nexus:
        named = "; ".join(f"s.{p.clause_num} ({p.heading}) — {nexus_phrase(p)}" for p in clauses_nexus)
        parts.append(f"touches the OSA/Ofcom machinery at {named}")
    return f"Flagged because the bill {'; and '.join(parts)}."


def to_flagged_bill(bill: TrackedBill) -> FlaggedBill:
    """Convert a flagged TrackedBill into its API response model.

    Args:
        bill: A bill with at least one flagged provision.

    Returns:
        The FlaggedBill response model, provisions ordered by descending triage score.
    """
    provisions = sorted(bill.provisions, key=lambda p: -p.triage_score)
    return FlaggedBill(
        bill_id=bill.bill_id,
        short_title=bill.short_title,
        carrier_family=bill.carrier_family.value,
        reason=build_reason(bill),
        max_triage_score=max(p.triage_score for p in bill.provisions),
        flagged_provisions=[
            FlaggedProvision(
                clause_num=provision.clause_num,
                heading=provision.heading,
                creates_offence=provision.creates_or_amends_offence,
                ofcom_nexus=sorted(signal.value for signal in provision.ofcom_nexus),
                matched_terms=provision.detection.matched_terms,
                triage_score=provision.triage_score,
            )
            for provision in provisions
        ],
    )


# --- App ------------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Scan and score the cache once at startup; hold the shortlist in app state."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    tracked = load_tracked_bills(CACHE_DIR, WEIGHTS)
    flagged = [bill for bill in tracked if bill.provisions]
    app.state.scanned = len(tracked)
    app.state.flagged = flagged
    logger.info("loaded %d bill(s) from %s; %d flagged", len(tracked), CACHE_DIR, len(flagged))
    yield


app = FastAPI(title=API_TITLE, version=API_VERSION, description=API_DESCRIPTION, lifespan=lifespan)


@app.get(
    "/flagged_bills",
    response_model=FlaggedBillsResponse,
    tags=["detection"],
    summary="Bills flagged as potential s.216A carriers",
)
def flagged_bills(
    request: Request,
    min_triage: float = Query(0.0, ge=0.0, description="Only return bills whose top clause scores at least this."),
) -> FlaggedBillsResponse:
    """Return every bill carrying at least one flagged clause, with the reason it was flagged.

    Args:
        request: The incoming request (used to read the cached shortlist from app state).
        min_triage: Optional floor on a bill's highest provision triage score.

    Returns:
        The flagged bills, highest-scoring first, each with a bill number and a reason.
    """
    flagged: list[TrackedBill] = request.app.state.flagged
    selected = [bill for bill in flagged if max(p.triage_score for p in bill.provisions) >= min_triage]
    selected.sort(key=lambda bill: -max(p.triage_score for p in bill.provisions))
    return FlaggedBillsResponse(
        scanned=request.app.state.scanned,
        count=len(selected),
        bills=[to_flagged_bill(bill) for bill in selected],
    )


@app.get("/", include_in_schema=False)
def root() -> dict[str, object]:
    """Minimal landing payload pointing at the Swagger docs."""
    return {"service": API_TITLE, "docs": "/docs", "endpoints": ["/flagged_bills"]}


if __name__ == "__main__":
    uvicorn.run(app, host=SERVE_HOST, port=SERVE_PORT)