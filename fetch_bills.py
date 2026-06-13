"""Fetch the XML of every live current-session UK Parliament bill into a local cache.

The cache is keyed by ``billId``: ``{cache_dir}/{billId}.xml``. Re-runs are idempotent
under two compatible meanings:

* *Existence-idempotent*: a cached file is reused unless the bill's ``lastUpdate``
  timestamp is newer than the file's mtime (i.e. the bill has been re-published).
* *Force mode*: ``--force`` ignores cache state and re-fetches everything.

Data is licensed under the Open Parliament Licence v3.0; this script writes a
``SOURCE.txt`` attribution file next to the snapshot.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click
import requests
from curl_cffi import requests as cffi_requests

# --- Endpoints -----------------------------------------------------------------------
# Bills API: ungated, sanctioned public API; serves bill metadata + publication index.
BILLS_API_BASE_URL = "https://bills-api.parliament.uk"
BILLS_LIST_PATH = "/api/v1/Bills"
BILL_PUBLICATIONS_PATH = "/api/v1/Bills/{bill_id}/Publications"

# Document host: Cloudflare-gated; serves the AKN XML files themselves.
BILLS_DOCUMENT_BASE_URL = "https://bills.parliament.uk"
PUBLICATION_DOCUMENT_PATH = "/publications/{publication_id}/documents/{file_id}"

# --- HTTP ----------------------------------------------------------------------------
# Honest UA on the API host; the document host requires browser impersonation regardless.
USER_AGENT = "uk-ai-legislation-tracker/0.1 (AI safety hackathon research project)"
REQUEST_TIMEOUT_SECONDS = 30
HTTP_OK = 200
PAGE_SIZE = 100  # /Bills page size; the API paginates via Skip/Take.

# --- Cloudflare impersonation --------------------------------------------------------
# Fingerprints confirmed via repeat probes to clear the document host. The bare "chrome"
# alias and "chrome146" were unreliable (intermittent / consistent 403s) so are excluded.
# Rotating across these prevents a single flagged fingerprint dropping a bill.
IMPERSONATE_TARGETS = ("chrome142", "firefox147", "safari184")
FETCH_RETRIES = 4
FETCH_BACKOFF_SECONDS = 1.0  # Doubles on each retry.
FETCH_DELAY_SECONDS = 1.0  # Pause between successful document fetches (politeness).

# --- AKN structure -------------------------------------------------------------------
# XML byte prefixes we accept as a real AKN document; anything else is a Cloudflare
# challenge page, a zipped .docx (OOXML's content type contains "xml"), or otherwise
# unusable, and must not enter the cache.
XML_VALID_PREFIXES = (b"<?xml", b"<akom")

# --- Output --------------------------------------------------------------------------
DEFAULT_CACHE_DIR = Path("data/raw_xml")
ATTRIBUTION_FILENAME = "SOURCE.txt"
OPL_ATTRIBUTION = (
    "Contains Parliamentary information licensed under the Open Parliament Licence v3.0.\n"
    "https://www.parliament.uk/site-information/copyright-parliament/open-parliament-licence/\n"
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchOutcome:
    """Per-bill result of a cache-builder run.

    Attributes:
        bill_id: The Parliament Bills API ``billId``.
        short_title: Human-readable bill title for logs.
        status: One of ``"saved"``, ``"cached"``, ``"refreshed"``, ``"no_xml"``,
            ``"fetch_failed"``.
        reason: Optional human-readable detail (only populated on failure/skip).
    """

    bill_id: int
    short_title: str
    status: str
    reason: str | None = None


def configure_logging(verbose: bool) -> None:
    """Install a single root handler at INFO (or DEBUG if ``verbose``).

    Args:
        verbose: If True, set the log level to DEBUG; otherwise INFO.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def fetch_all_bills() -> list[dict[str, Any]]:
    """Fetch every bill from the Bills API by paginating until exhausted.

    Pagination is defensive: it advances by the number of items actually returned
    (survives a server-capped page size) and stops when a page yields no new bill IDs
    (survives ``Skip`` being ignored). The API host is ungated so plain requests suffices.

    Returns:
        A list of raw bill dicts as returned by the API.

    Raises:
        requests.HTTPError: If the API returns a non-2xx response.
    """
    all_bills: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    skip = 0
    while True:
        response = requests.get(
            f"{BILLS_API_BASE_URL}{BILLS_LIST_PATH}",
            params={"Take": PAGE_SIZE, "Skip": skip},
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        items = response.json().get("items", [])
        new_items = [bill for bill in items if bill.get("billId") not in seen_ids]
        if not new_items:
            return all_bills
        seen_ids.update(bill["billId"] for bill in new_items)
        all_bills.extend(new_items)
        skip += len(items)
        logger.debug("fetched page: %d new bills (total %d)", len(new_items), len(all_bills))


def derive_current_session_id(bills: list[dict[str, Any]]) -> int:
    """Return the largest ``introducedSessionId`` present across the bills list.

    Session ids increase monotonically over time, so the current session is the max.
    Deriving rather than hardcoding keeps the script correct across prorogations.

    Args:
        bills: Output of :func:`fetch_all_bills`.

    Returns:
        The current Parliament session id.

    Raises:
        ValueError: If no bill in the list has an ``introducedSessionId``.
    """
    session_ids = [bill.get("introducedSessionId") for bill in bills if bill.get("introducedSessionId") is not None]
    if not session_ids:
        raise ValueError("No introducedSessionId on any bill; cannot derive current session.")
    return max(session_ids)


def filter_live_session_bills(bills: list[dict[str, Any]], session_id: int) -> list[dict[str, Any]]:
    """Filter to bills that are in the given session and still live.

    Live means: not withdrawn, not defeated, not yet an Act.

    Args:
        bills: Full bills list.
        session_id: Session to scope to (typically from
            :func:`derive_current_session_id`).

    Returns:
        Filtered list, original order preserved.
    """
    in_session = [
        bill
        for bill in bills
        if session_id in (bill.get("includedSessionIds") or []) or bill.get("introducedSessionId") == session_id
    ]
    return [
        bill
        for bill in in_session
        if bill.get("billWithdrawn") is None and not bill.get("isDefeated", False) and not bill.get("isAct", False)
    ]


def looks_like_akn_xml(filename: str | None, content_type: str | None) -> bool:
    """Decide whether a publication file is a real AKN XML document.

    Excludes OOXML (``.docx`` etc.) whose content type contains the substring "xml"
    (``application/vnd.openxmlformats-officedocument...``) but which is actually a zip.

    Args:
        filename: Publication file's reported filename, if any.
        content_type: Publication file's reported MIME type, if any.

    Returns:
        True if the file is plausibly AKN XML; False otherwise.
    """
    filename_lower = (filename or "").lower()
    content_type_lower = (content_type or "").lower()
    if filename_lower.endswith(".xml"):
        return True
    return "xml" in content_type_lower and "officedocument" not in content_type_lower


def resolve_xml_ids(bill_id: int) -> tuple[int | None, int | None]:
    """Find the publication and file IDs of a bill's AKN XML document, if any.

    Hedges across the two known Publications response shapes (top-level
    ``publications``/``items``; nested ``files``/``documents``).

    Args:
        bill_id: The Parliament ``billId``.

    Returns:
        A pair ``(publication_id, file_id)``. Both ``None`` if no AKN XML is listed.

    Raises:
        requests.HTTPError: If the API returns a non-2xx response.
    """
    response = requests.get(
        f"{BILLS_API_BASE_URL}{BILL_PUBLICATIONS_PATH.format(bill_id=bill_id)}",
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    records = payload.get("publications") or payload.get("items") or []
    for record in records:
        for document in record.get("files") or record.get("documents") or []:
            if looks_like_akn_xml(document.get("filename"), document.get("contentType")):
                return record.get("id"), document.get("id")
    return None, None


def fetch_document_bytes(publication_id: int, file_id: int) -> bytes | None:
    """Fetch a publication document from the gated host, rotating impersonation targets.

    A 200 alone is not enough: Cloudflare returns 200s for some challenge/redirect
    flows, so we additionally require the body to start with an AKN-XML prefix.
    Backoff doubles between retries.

    Args:
        publication_id: Publication record id from :func:`resolve_xml_ids`.
        file_id: File id within that publication.

    Returns:
        Document bytes on success; ``None`` if all retries are exhausted or every
        response was challenged.
    """
    path = PUBLICATION_DOCUMENT_PATH.format(publication_id=publication_id, file_id=file_id)
    url = f"{BILLS_DOCUMENT_BASE_URL}{path}"
    delay = FETCH_BACKOFF_SECONDS
    for attempt in range(FETCH_RETRIES):
        target = IMPERSONATE_TARGETS[attempt % len(IMPERSONATE_TARGETS)]
        try:
            response = cffi_requests.get(url, impersonate=target, timeout=REQUEST_TIMEOUT_SECONDS)
        except Exception as error:  # noqa: BLE001 — broad on purpose: any transport error is retryable.
            logger.debug("fetch attempt %d (%s) raised: %s", attempt + 1, target, error)
            time.sleep(delay)
            delay *= 2
            continue
        body_prefix = response.content.lstrip()[:5]
        if response.status_code == HTTP_OK and body_prefix in XML_VALID_PREFIXES:
            return response.content
        logger.debug(
            "fetch attempt %d (%s) rejected: status=%s prefix=%r",
            attempt + 1,
            target,
            response.status_code,
            body_prefix,
        )
        time.sleep(delay)
        delay *= 2
    return None


def parse_last_update(timestamp: str | None) -> datetime | None:
    """Parse an API ``lastUpdate`` ISO string into an aware UTC datetime.

    The API returns naive timestamps in UTC (e.g. ``2025-09-16T17:08:18.2184786``);
    we attach UTC explicitly so comparisons with file mtimes are well-defined.

    Args:
        timestamp: The raw string, or None.

    Returns:
        An aware UTC datetime, or None if input is None or unparseable.
    """
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        logger.debug("could not parse lastUpdate: %r", timestamp)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def cache_is_current(cache_path: Path, last_update: datetime | None) -> bool:
    """Return True if the cached file is at least as new as the bill's last update.

    Args:
        cache_path: Path to the cached ``{billId}.xml``.
        last_update: Parsed ``lastUpdate`` timestamp from the API, or None.

    Returns:
        True iff the cache file exists and (no last_update is known, or its mtime
        is at or after ``last_update``).
    """
    if not cache_path.exists():
        return False
    if last_update is None:
        return True
    mtime = datetime.fromtimestamp(cache_path.stat().st_mtime, tz=timezone.utc)
    return mtime >= last_update


def cache_one_bill(bill: dict[str, Any], cache_dir: Path, force: bool) -> FetchOutcome:
    """Ensure the cache holds the current XML for one bill.

    Args:
        bill: Raw bill record from the API.
        cache_dir: Directory to write into.
        force: If True, ignore existing cache and re-fetch.

    Returns:
        A :class:`FetchOutcome` describing what happened.
    """
    bill_id = bill["billId"]
    short_title = bill["shortTitle"]
    cache_path = cache_dir / f"{bill_id}.xml"
    last_update = parse_last_update(bill.get("lastUpdate"))

    if not force and cache_is_current(cache_path, last_update):
        return FetchOutcome(bill_id, short_title, "cached")

    publication_id, file_id = resolve_xml_ids(bill_id)
    if file_id is None or publication_id is None:
        return FetchOutcome(bill_id, short_title, "no_xml", reason="no AKN XML in publications")

    xml_bytes = fetch_document_bytes(publication_id, file_id)
    if xml_bytes is None:
        return FetchOutcome(bill_id, short_title, "fetch_failed", reason="all retries exhausted or challenged")

    cache_path.write_bytes(xml_bytes)
    status = "refreshed" if cache_path.exists() and not force and last_update is not None else "saved"
    return FetchOutcome(bill_id, short_title, status)


def write_attribution(cache_dir: Path) -> None:
    """Write the OPL v3.0 attribution notice into ``cache_dir``.

    Args:
        cache_dir: Directory the snapshot is written to.
    """
    (cache_dir / ATTRIBUTION_FILENAME).write_text(OPL_ATTRIBUTION, encoding="utf-8")


def run(cache_dir: Path, force: bool, limit: int | None) -> list[FetchOutcome]:
    """Drive the full snapshot: discover live bills and cache their XML.

    Args:
        cache_dir: Directory to write the snapshot into; created if absent.
        force: If True, re-fetch every bill regardless of cache state.
        limit: If set, attempt only the first ``limit`` live bills (for smoke tests).

    Returns:
        Per-bill outcomes in attempt order.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    write_attribution(cache_dir)

    logger.info("fetching bills index from %s", BILLS_API_BASE_URL)
    all_bills = fetch_all_bills()
    current_session_id = derive_current_session_id(all_bills)
    live_bills = filter_live_session_bills(all_bills, current_session_id)
    logger.info(
        "indexed %d bills; current session = %d; %d live in session",
        len(all_bills),
        current_session_id,
        len(live_bills),
    )

    bills_to_attempt = live_bills if limit is None else live_bills[:limit]
    outcomes: list[FetchOutcome] = []
    for bill in bills_to_attempt:
        outcome = cache_one_bill(bill, cache_dir, force=force)
        outcomes.append(outcome)
        logger.info("%-12s %5d  %s", outcome.status, outcome.bill_id, outcome.short_title)
        if outcome.status in {"saved", "refreshed"}:
            time.sleep(FETCH_DELAY_SECONDS)
    return outcomes


def summarise(outcomes: list[FetchOutcome]) -> dict[str, int]:
    """Tally outcomes by status.

    Args:
        outcomes: Result of :func:`run`.

    Returns:
        Mapping of status -> count, including any status with zero occurrences.
    """
    statuses = ("saved", "refreshed", "cached", "no_xml", "fetch_failed")
    return {status: sum(1 for outcome in outcomes if outcome.status == status) for status in statuses}


@click.command()
@click.option(
    "--cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_CACHE_DIR,
    show_default=True,
    help="Directory to write the bill XML snapshot into.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Re-fetch every live bill regardless of cache state.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Attempt only the first N live bills (smoke testing).",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable debug logging.",
)
def main(cache_dir: Path, force: bool, limit: int | None, verbose: bool) -> None:
    """Snapshot live current-session UK Parliament bill XML to ``cache_dir``."""
    configure_logging(verbose)
    outcomes = run(cache_dir=cache_dir, force=force, limit=limit)
    totals = summarise(outcomes)
    click.echo(
        f"\nsnapshot complete: "
        f"{totals['saved']} saved, "
        f"{totals['refreshed']} refreshed, "
        f"{totals['cached']} already current, "
        f"{totals['no_xml']} without XML, "
        f"{totals['fetch_failed']} failed"
    )
    if totals["fetch_failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()