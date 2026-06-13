"""Flag bills: which cached AKN bills carry s.216A-reachable provisions?

It computes ONLY the machine-detected signals — offence creation, and whether the offence
could be affected by AI. 

Run:
    python flag_bills.py data/raw_xml
"""

from __future__ import annotations

import argparse
import logging
import re
from datetime import date, datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from models import (
    CandidateProvision,
    CarrierFamily,
    DetectionEvidence,
    OfcomNexusSignal,
    ScoringWeights,
    TrackedBill,
)

logger = logging.getLogger(__name__)

# --- AKN namespace -------------------------------------------------------------------
# UK bills are Akoma Ntoso 3.0; the default namespace is the OASIS AKN 3.0 URI. Every
# element is namespaced, so findall/iter must qualify tags with it.
AKN_NS = "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"
SECTION_TAG = f"{{{AKN_NS}}}section"
NUM_TAG = f"{{{AKN_NS}}}num"
HEADING_TAG = f"{{{AKN_NS}}}heading"
LONG_TITLE_TAG = f"{{{AKN_NS}}}longTitle"
QUOTED_STRUCTURE_TAG = f"{{{AKN_NS}}}quotedStructure"
TLC_CONCEPT_TAG = f"{{{AKN_NS}}}TLCConcept"
TLC_PROCESS_TAG = f"{{{AKN_NS}}}TLCProcess"
FRBR_DATE_TAG = f"{{{AKN_NS}}}FRBRdate"

# Term-list version stamped onto every DetectionEvidence (provenance for week-on-week diffs).
TERM_LIST_VERSION = "smoke-0.1"

# Files in the cache dir that are not bills (fetch_bills.py writes an attribution file).
NON_BILL_FILENAMES = frozenset({"SOURCE.txt"})

# --- Signal 1: offence creation/amendment --------------------------------------------
# High-precision phrases marking a clause that creates or amends a criminal offence. This
# is the Stage 0 net (replacing AI-term regex). Deliberately conservative to keep
# precision high — recall is recovered at human review, not by loosening these.
OFFENCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("commits an offence", re.compile(r"commits an offence", re.IGNORECASE)),
    ("guilty of an offence", re.compile(r"guilty of an offence", re.IGNORECASE)),
    ("liable on conviction", re.compile(r"liable[, ][^.]{0,40}conviction", re.IGNORECASE)),
    ("imprisonment for a term", re.compile(r"imprisonment for a term", re.IGNORECASE)),
)

# --- Signal 2: OSA / Ofcom nexus -----------------------------------------------------
# Maps each structural signal in models.OfcomNexusSignal to its detecting pattern.
# NOTE: "Schedule 7" is only meaningful in Online Safety Act context (the priority-
# offences schedule); unrelated Acts have their own Schedule 7, so this signal is the
# weakest and is starred as review-only in the output.
NEXUS_PATTERNS: tuple[tuple[OfcomNexusSignal, re.Pattern[str]], ...] = (
    (OfcomNexusSignal.AMENDS_ONLINE_SAFETY_ACT, re.compile(r"Online Safety Act 2023", re.IGNORECASE)),
    (OfcomNexusSignal.AMENDS_COMMUNICATIONS_ACT, re.compile(r"Communications Act 2003", re.IGNORECASE)),
    (OfcomNexusSignal.MENTIONS_OFCOM, re.compile(r"\bOFCOM\b", re.IGNORECASE)),
    (OfcomNexusSignal.MENTIONS_PRIORITY_OFFENCE, re.compile(r"priority (?:offence|illegal content)", re.IGNORECASE)),
    (OfcomNexusSignal.MENTIONS_SCHEDULE_7, re.compile(r"Schedule 7", re.IGNORECASE)),
)

# --- Signal 3: carrier family --------------------------------------------------------
# Coarse title / long-title keyword buckets. Carrier-fit is a soft admissibility signal,
# not a hard gate (OTHER is recorded, never dropped). Order matters: first match wins.
CARRIER_KEYWORDS: tuple[tuple[CarrierFamily, tuple[str, ...]], ...] = (
    (CarrierFamily.ONLINE_HARMS, ("online safety", "online harms", "communications")),
    (CarrierFamily.CRIMINAL_JUSTICE, ("criminal justice", "sentencing", "courts")),
    (CarrierFamily.CRIME_POLICING, ("crime", "policing", "police")),
    (CarrierFamily.NATIONAL_SECURITY, ("national security", "terrorism", "counter-terror")),
    (CarrierFamily.DATA, ("data protection", "data (use and access", "digital information")),
)

# Best-effort "<Name> Act <year>" harvester for amends_acts (carrier-classification input).
AMENDED_ACT_RE = re.compile(r"[A-Z][A-Za-z’'()\- ]+ Act \d{4}")
WHITESPACE_RE = re.compile(r"\s+")


def parse_bill_id(stem: str) -> int:
    """Best-effort billId from a filename stem.

    fetch_bills.py names cache files ``{billId}.xml``, so in the real cache the stem is the
    integer billId. Demo/synthetic fixtures use non-numeric stems and get a 0 placeholder
    (logged), since TrackedBill.bill_id is typed int.
    """
    if stem.isdigit():
        return int(stem)
    logger.debug("non-numeric stem %r; using bill_id=0 placeholder", stem)
    return 0


def normalise(text: str) -> str:
    """Collapse AKN whitespace (newlines and <?L ...?> line-break PIs leave ragged spacing)."""
    return WHITESPACE_RE.sub(" ", text).strip()


def element_text(element: ET.Element) -> str:
    """All descendant text of an element, whitespace-normalised.

    itertext() merges the character data either side of AKN's <?L ...?> line-break
    processing instructions, so clause text reads continuously.
    """
    return normalise("".join(element.itertext()))


def first_text(element: ET.Element, tag: str) -> str:
    """Text of the first direct child with ``tag``, or "" if absent."""
    child = element.find(tag)
    return normalise("".join(child.itertext())) if child is not None else ""


def build_parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    """Child -> parent map (ElementTree has no parent pointers or ancestor axis)."""
    return {child: parent for parent in root.iter() for child in parent}


def is_inside_quoted_structure(element: ET.Element, parent_map: dict[ET.Element, ET.Element]) -> bool:
    """True if any ancestor is a <quotedStructure> (text being inserted into another Act).

    Such sections are quoted amendment text, not free-standing provisions of this bill, so
    they are not emitted as their own CandidateProvision. Their text is still scanned,
    because it is part of the enclosing section's itertext.
    """
    node = parent_map.get(element)
    while node is not None:
        if node.tag == QUOTED_STRUCTURE_TAG:
            return True
        node = parent_map.get(node)
    return False


def detect_offence(text: str) -> tuple[bool, list[str]]:
    """Signal 1: did any offence-creation pattern fire? Returns (fired, matched_labels)."""
    matched = [label for label, pattern in OFFENCE_PATTERNS if pattern.search(text)]
    return bool(matched), matched


def detect_nexus(text: str) -> tuple[frozenset[OfcomNexusSignal], list[str]]:
    """Signal 2: which OSA/Ofcom nexus signals fired? Returns (signals, matched_labels)."""
    signals: list[OfcomNexusSignal] = []
    labels: list[str] = []
    for signal, pattern in NEXUS_PATTERNS:
        if pattern.search(text):
            signals.append(signal)
            labels.append(signal.value)
    return frozenset(signals), labels


def classify_carrier(title: str) -> CarrierFamily:
    """Signal 3: coarse carrier-family bucket from the bill title / long title."""
    lowered = title.lower()
    for family, keywords in CARRIER_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return family
    return CarrierFamily.OTHER


def compute_triage_score(
    creates_offence: bool,
    nexus: frozenset[OfcomNexusSignal],
    carrier: CarrierFamily,
    weights: ScoringWeights,
) -> float:
    """Provision-level triage score from the machine signals, using the model's weights."""
    score = weights.offence_signal if creates_offence else 0.0
    score += weights.per_nexus_signal * len(nexus)
    if carrier is not CarrierFamily.OTHER:
        score += weights.carrier_fit
    return score


def parse_published_date(root: ET.Element) -> date:
    """Bill publication date from the first FRBRdate name="published", or today as fallback."""
    node = root.find(f".//{FRBR_DATE_TAG}[@name='published']")
    if node is None:
        return date.today()
    raw = (node.get("date") or "").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).astimezone(timezone.utc).date()
    except ValueError:
        logger.debug("unparseable FRBRdate %r; falling back to today", raw)
        return date.today()


def tlc_show_as(root: ET.Element, tag: str, eid: str) -> str:
    """showAs of a TLC element selected by eId, or "" if absent (bill-level metadata)."""
    node = root.find(f".//{tag}[@eId='{eid}']")
    return (node.get("showAs") if node is not None else "") or ""


def extract_bill(path: Path, weights: ScoringWeights) -> TrackedBill | None:
    """Parse one AKN file into a TrackedBill with its flagged CandidateProvisions.

    Returns None if the file cannot be parsed (logged, not raised) so one corrupt or
    truncated bill never aborts a whole snapshot.
    """
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        logger.error("skipping %s: XML parse failed (%s)", path.name, error)
        return None

    short_title = tlc_show_as(root, TLC_CONCEPT_TAG, "varBillTitle") or path.stem
    stage = tlc_show_as(root, TLC_PROCESS_TAG, "varStageVersion")
    long_title_el = root.find(f".//{LONG_TITLE_TAG}")
    long_title = element_text(long_title_el) if long_title_el is not None else ""
    carrier = classify_carrier(f"{short_title} {long_title}")

    parent_map = build_parent_map(root)
    full_text = element_text(root)
    amends_acts = sorted({normalise(m) for m in AMENDED_ACT_RE.findall(full_text)})

    provisions: list[CandidateProvision] = []
    for section in root.iter(SECTION_TAG):
        if is_inside_quoted_structure(section, parent_map):
            continue
        num = first_text(section, NUM_TAG)
        heading = first_text(section, HEADING_TAG)
        text = element_text(section)

        creates_offence, offence_terms = detect_offence(text)
        nexus, nexus_terms = detect_nexus(text)
        if not creates_offence and not nexus:
            continue  # only offence-creating or Ofcom-facing clauses make the shortlist

        provisions.append(
            CandidateProvision(
                provision_id=f"{parse_bill_id(path.stem)}:{num}",
                clause_num=num,
                heading=heading,
                text=text,
                creates_or_amends_offence=creates_offence,
                detection=DetectionEvidence(
                    term_list_version=TERM_LIST_VERSION,
                    matched_terms=offence_terms + nexus_terms,
                ),
                ofcom_nexus=nexus,
                triage_score=compute_triage_score(creates_offence, nexus, carrier, weights),
                # assessment / priority_score deliberately left None — human review only.
            )
        )

    return TrackedBill(
        bill_id=parse_bill_id(path.stem),
        short_title=short_title,
        long_title=long_title,
        carrier_family=carrier,
        stage=stage,
        last_update=parse_published_date(root),
        snapshot_date=date.today(),
        amends_acts=amends_acts,
        provisions=provisions,
    )


def bill_nexus(bill: TrackedBill) -> frozenset[OfcomNexusSignal]:
    """Union of nexus signals across a bill's flagged provisions (bill-level roll-up)."""
    signals: set[OfcomNexusSignal] = set()
    for provision in bill.provisions:
        signals |= set(provision.ofcom_nexus)
    return frozenset(signals)


def report(bills: list[TrackedBill], scanned: int, skipped: int) -> None:
    """Print the triage-ranked shortlist (CLI presentation layer)."""
    flagged_bills = [bill for bill in bills if bill.provisions]
    print(f"\nScanned {scanned} file(s): {len(bills)} parsed, {skipped} skipped.")
    print(f"{len(flagged_bills)} of {len(bills)} parsed bill(s) carry >=1 s.216A-candidate provision.\n")

    for bill in sorted(flagged_bills, key=lambda b: -max(p.triage_score for p in b.provisions)):
        signals = ", ".join(sorted(s.value for s in bill_nexus(bill))) or "(none)"
        print(f"=== billId {bill.bill_id} | {bill.short_title}")
        print(f"    carrier_family : {bill.carrier_family.value}")
        print(f"    stage          : {bill.stage or '(unknown)'}")
        print(f"    bill nexus     : {signals}")
        print(f"    amends_acts    : {', '.join(bill.amends_acts) or '(none harvested)'}")
        print(f"    flagged clauses: {len(bill.provisions)}")
        for provision in sorted(bill.provisions, key=lambda p: -p.triage_score):
            star = " *" if OfcomNexusSignal.MENTIONS_SCHEDULE_7 in provision.ofcom_nexus else ""
            offence = "offence" if provision.creates_or_amends_offence else "-"
            nexus = ",".join(sorted(s.value.replace("mentions_", "").replace("amends_", "+") for s in provision.ofcom_nexus)) or "-"
            print(
                f"      [{provision.triage_score:>4.1f}] s.{provision.clause_num:<4} {provision.heading[:46]:<46} "
                f"| {offence:<7} | {nexus}{star}"
            )
        print()

    if any(OfcomNexusSignal.MENTIONS_SCHEDULE_7 in p.ofcom_nexus for b in flagged_bills for p in b.provisions):
        print('  * "Schedule 7" is OSA-meaningful only; verify it is the Online Safety Act schedule, not the bill\'s own.\n')


def main() -> None:
    """Smoke-test entry point: scan a directory of cached AKN bills and rank carriers."""
    parser = argparse.ArgumentParser(description="s.216A carrier detection smoke test over cached AKN bills.")
    parser.add_argument("cache_dir", type=Path, help="Directory of {billId}.xml AKN files (e.g. data/raw_xml).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    weights = ScoringWeights()
    xml_paths = sorted(p for p in args.cache_dir.glob("*.xml") if p.name not in NON_BILL_FILENAMES)
    logger.info("scanning %d xml file(s) in %s", len(xml_paths), args.cache_dir)

    bills: list[TrackedBill] = []
    skipped = 0
    for path in xml_paths:
        bill = extract_bill(path, weights)
        if bill is None:
            skipped += 1
            continue
        bills.append(bill)
        logger.info("parsed %-28s carrier=%-13s flagged=%d", path.name, bill.carrier_family.value, len(bill.provisions))

    report(bills, scanned=len(xml_paths), skipped=skipped)


if __name__ == "__main__":
    main()