"""FastAPI service exposing the flagged-bills query for the AI-offence bill tracker.

A bill is flagged when its text mentions BOTH a criminal-offence term AND a term
suggesting the conduct could feasibly be carried out using AI (see models.py). The cache
of AKN bills is parsed and scored once at startup; the flagged shortlist is held in app
state (a single shared instance) and ranked by total keyword points, highest first.
Restart to re-scan after refreshing the cache with fetch_bills.py.

NOTE — new dependencies: this module takes on third-party libraries (``fastapi``,
``uvicorn``); the standard library cannot generate the Swagger schema. Install with
``pip install fastapi uvicorn``.

Run:  python serve.py        (or)   uvicorn serve:app --reload
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

import uvicorn
from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from models import ScoredBill, score_bill

logger = logging.getLogger(__name__)

# --- Configuration -------------------------------------------------------------------
# Cache dir holding {billId}.xml AKN files (written by fetch_bills.py). Overridable via an
# env var so the service can point at a real cache without editing code.
DEFAULT_CACHE_DIR = Path("data/raw_xml")
CACHE_DIR = Path(os.environ.get("S216_CACHE_DIR", str(DEFAULT_CACHE_DIR)))

# Files in the cache dir that are not bills (fetch_bills.py writes an attribution file).
NON_BILL_FILENAMES = frozenset({"SOURCE.txt"})

# --- AKN parsing ---------------------------------------------------------------------
# UK bills are Akoma Ntoso 3.0; the default namespace is the OASIS AKN 3.0 URI, so element
# lookups must be namespace-qualified.
AKN_NS = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"
BODY_TAG = f"{{{AKN_NS}}}body"
TLC_CONCEPT_TAG = f"{{{AKN_NS}}}TLCConcept"
BILL_TITLE_EID = "varBillTitle"  # TLCConcept whose showAs carries the bill short title.
WHITESPACE_RE = re.compile(r"\s+")

# --- API metadata (surfaced in Swagger UI) -------------------------------------------
API_TITLE = "UK AI Bill Tracker"
API_VERSION = "0.2.0"
API_DESCRIPTION = (
    "Returns UK Parliament bills flagged as relevant to AI-enabled offending: bills whose "
    "text mentions both a criminal-offence term and a term suggesting the conduct could be "
    "carried out using AI. Each result gives the bill number, name, the reasons it was "
    "flagged, and the keywords it mentioned. Bills are ranked by total keyword points."
)

# --- uvicorn run defaults (used only by the __main__ convenience runner) -------------
SERVE_HOST = "127.0.0.1"
SERVE_PORT = 8000


# --- Response schema (documented in Swagger) -----------------------------------------


class ReturnedBill(BaseModel):
    """A flagged bill, and why it was flagged."""

    bill_id: int = Field(..., description="Parliament Bills API billId (the cache filename stem).")
    bill_name: str = Field(..., description="Bill short title.")
    reasons_flagged: list[str] = Field(..., description="Why the bill was flagged, one reason per criterion met.")
    keywords_mentioned: list[str] = Field(..., description="Distinct keywords found, criminal terms first then AI.")


# --- Cache loading + response transform ----------------------------------------------


def extract_text(path: Path) -> tuple[int, str, str] | None:
    """Parse an AKN file into (bill_id, bill_name, body_text).

    Scoring runs over the <body> only, so the cover page, table of contents and metadata
    do not inflate keyword counts. Returns None if the file cannot be parsed, so one
    corrupt or truncated bill never aborts the whole scan.

    Args:
        path: Path to a {billId}.xml AKN file.

    Returns:
        A (bill_id, bill_name, text) triple, or None if parsing failed.
    """
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        logger.error("skipping %s: XML parse failed (%s)", path.name, error)
        return None
    title = root.find(f".//{TLC_CONCEPT_TAG}[@eId='{BILL_TITLE_EID}']")
    bill_name = (title.get("showAs") if title is not None else "") or path.stem
    body = root.find(f".//{BODY_TAG}")
    raw_text = " ".join((body if body is not None else root).itertext())
    text = WHITESPACE_RE.sub(" ", raw_text).strip()
    bill_id = int(path.stem) if path.stem.isdigit() else 0
    return bill_id, bill_name, text


def load_scored_bills(cache_dir: Path) -> list[ScoredBill]:
    """Parse and score every AKN file in the cache directory.

    Args:
        cache_dir: Directory of {billId}.xml files.

    Returns:
        One ScoredBill per parseable file (unparseable files are skipped).
    """
    scored: list[ScoredBill] = []
    for path in sorted(cache_dir.glob("*.xml")):
        if path.name in NON_BILL_FILENAMES:
            continue
        extracted = extract_text(path)
        if extracted is None:
            continue
        bill_id, bill_name, text = extracted
        scored.append(score_bill(bill_id, bill_name, text))
    return scored


def to_returned_bill(bill: ScoredBill) -> ReturnedBill:
    """Convert a flagged ScoredBill into its API response model.

    Args:
        bill: A flagged ScoredBill.

    Returns:
        A ReturnedBill with the bill number, name, reasons and keywords.
    """
    criminal_keywords = ", ".join(hit.keyword for hit in bill.criminal_hits)
    ai_keywords = ", ".join(hit.keyword for hit in bill.ai_hits)
    return ReturnedBill(
        bill_id=bill.bill_id,
        bill_name=bill.bill_name,
        reasons_flagged=[
            f"Mentions a criminal offence ({bill.criminal_score} keyword hit(s): {criminal_keywords}).",
            f"Could feasibly involve AI ({bill.ai_score} keyword hit(s): {ai_keywords}).",
        ],
        keywords_mentioned=bill.matched_keywords,
    )


# --- App ------------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Scan and score the cache once at startup; hold the ranked shortlist in app state."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    scored = load_scored_bills(CACHE_DIR)
    flagged = sorted((bill for bill in scored if bill.is_flagged), key=lambda bill: -bill.total_score)
    app.state.flagged = flagged
    logger.info("scored %d bill(s) from %s; %d flagged", len(scored), CACHE_DIR, len(flagged))
    yield


app = FastAPI(title=API_TITLE, version=API_VERSION, description=API_DESCRIPTION, lifespan=lifespan)


@app.get(
    "/flagged_bills",
    response_model=list[ReturnedBill],
    tags=["detection"],
    summary="Bills flagged as relevant to AI-enabled offending",
)
def flagged_bills(request: Request) -> list[ReturnedBill]:
    """Return flagged bills (criminal-offence term AND AI term), highest keyword score first.

    Args:
        request: The incoming request (used to read the cached shortlist from app state).

    Returns:
        The flagged bills, each with its number, name, reasons and keywords.
    """
    flagged: list[ScoredBill] = request.app.state.flagged
    return [to_returned_bill(bill) for bill in flagged]


@app.get("/", include_in_schema=False)
def root() -> dict[str, object]:
    """Minimal landing payload pointing at the Swagger docs."""
    return {"service": API_TITLE, "docs": "/docs", "endpoints": ["/flagged_bills"]}


if __name__ == "__main__":
    uvicorn.run(app, host=SERVE_HOST, port=SERVE_PORT)