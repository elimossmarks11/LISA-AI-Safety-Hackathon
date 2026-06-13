"""Data models for the s.216A carrier tracker.

A bill carries machine-detected structural signals (carrier family, offence-creating
clauses, Ofcom/OSA nexus) used to build a review shortlist. Each candidate provision is
then human-verified against the three-filter reachability rubric shared with the manual
cyber/bio analysis. Standard-library only — no new dependency.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum, IntEnum


# --- Categorical vocabularies ---------------------------------------------------------

class CarrierFamily(str, Enum):
    """Bill-type families where an AI-misuse amendment reads as in-scope (admissibility)."""

    CRIME_POLICING = "crime_policing"
    CRIMINAL_JUSTICE = "criminal_justice"
    ONLINE_HARMS = "online_harms"
    DATA = "data"
    NATIONAL_SECURITY = "national_security"
    OTHER = "other"  # recorded, not dropped — carrier-fit is a soft filter, not a hard gate


class HarmDomain(str, Enum):
    """The specific harm a provision could anchor (mirrors the worked-examples areas)."""

    CYBER = "cyber"
    FRAUD = "fraud"
    CSEA = "csea"
    TERRORISM = "terrorism"
    BIO_CBRN = "bio_cbrn"
    HARASSMENT = "harassment"
    OTHER = "other"


class OfcomNexusSignal(str, Enum):
    """Auto-detected structural signals that a clause touches the OSA/Ofcom machinery."""

    AMENDS_ONLINE_SAFETY_ACT = "amends_online_safety_act"
    AMENDS_COMMUNICATIONS_ACT = "amends_communications_act"
    MENTIONS_OFCOM = "mentions_ofcom"
    MENTIONS_PRIORITY_OFFENCE = "mentions_priority_offence"
    MENTIONS_SCHEDULE_7 = "mentions_schedule_7"


class InsertionType(str, Enum):
    """How a provision could be drawn into the s.216A regime, in descending leverage order."""

    FACILITABLE_OFFENCE = "facilitable_offence"  # best: offence an AI service can facilitate, addable to Sch 7
    OFCOM_DUTY_OR_POWER = "ofcom_duty_or_power"  # writes a duty directly, skips priority-list step
    SCOPE_OR_DEFINITION_TWEAK = "scope_or_definition_tweak"  # widens what existing machinery reaches


class ActivationPath(str, Enum):
    """The downstream statutory route needed to switch on a branch-(b) duty."""

    ACTIVATE_ONLY = "activate_only"  # anchor already in priority scope; needs s.216A switch-on only
    ADD_TO_SCHEDULE_7_THEN_ACTIVATE = "add_to_schedule_7_then_activate"  # SI to list, then s.216A
    DIRECT_DUTY = "direct_duty"  # bill writes the Ofcom duty itself; no priority-list step
    OUT_OF_REACH = "out_of_reach"  # no viable path (e.g. harm is societal/abroad) — recorded dead-end


class Rating(IntEnum):
    """Ordinal strength for a single filter. Labels match the cyber/bio table vocabulary.

    IntEnum so the composite score can sum levels directly; see ScoringWeights for the
    (linear) simplification this implies.
    """

    NONE = 0
    LOW = 1
    MODERATE = 2
    HIGH = 3
    VERY_HIGH = 4
    MAXIMAL = 5


class ReviewStatus(str, Enum):
    """Human-review lifecycle for a candidate provision."""

    PENDING = "pending"  # auto-detected, not yet looked at
    REVIEWED = "reviewed"  # human has assessed the filters
    DISMISSED = "dismissed"  # human judged it a non-candidate; kept so it doesn't resurface


# --- Evidence and assessment ----------------------------------------------------------

@dataclass
class DetectionEvidence:
    """Provenance for the machine-detected signals on a provision.

    Attributes:
        term_list_version: Version of the offence/nexus term list that produced the matches.
        matched_terms: The literal terms/patterns that fired.
        matched_spans: Clause-local (start, end) character offsets for each match, for highlighting.
    """

    term_list_version: str
    matched_terms: list[str] = field(default_factory=list)
    matched_spans: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class FilterAssessment:
    """A single human-verified filter rating with its justification.

    Attributes:
        rating: Ordinal strength, or None until a human has assessed it.
        rationale: The reviewer's reasoning for the rating.
        citation: Clause reference (num/heading) backing the rating, for verifiability.
    """

    rating: Rating | None = None
    rationale: str = ""
    citation: str = ""


@dataclass
class ReachabilityAssessment:
    """The three-filter rubric, human-verified, shared with the manual analysis.

    facilitation_fit + individual_uk_harm establish *legal reachability*;
    prevalence + severity establish *political tractability* (plausibility, split because
    the bio case has extreme severity but near-nil prevalence). The two downstream
    audiences weight these groups differently — see ScoringWeights.

    Attributes:
        facilitation_fit: Can an AI service facilitate (or be used to commit) the offence via its outputs/actions?
        individual_uk_harm: Does it satisfy the locked purpose clause (UK-individual harm)?
        prevalence: How common is the harm today?
        severity: How serious is the harm?
        reviewed_by: Identifier of the reviewer who verified this assessment.
        reviewed_at: When the assessment was verified.
    """

    facilitation_fit: FilterAssessment = field(default_factory=FilterAssessment)
    individual_uk_harm: FilterAssessment = field(default_factory=FilterAssessment)
    prevalence: FilterAssessment = field(default_factory=FilterAssessment)
    severity: FilterAssessment = field(default_factory=FilterAssessment)
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


# --- Core records ---------------------------------------------------------------------

@dataclass
class CandidateProvision:
    """One offence-creating or Ofcom-facing clause, scored as a carrier candidate.

    Fields divide into machine-detected (set by the pipeline) and human-verified (set at
    review). Maps to one row of the cyber/bio worked-examples table.

    Attributes:
        provision_id: Stable id for week-on-week diffing (e.g. f"{bill_id}:{clause_num}").
            NOTE: clause renumbering between bill versions can break this — see design notes.
        clause_num: The AKN <num> for this clause.
        heading: The AKN <heading> for this clause.
        text: The clause text under assessment.
        creates_or_amends_offence: Machine signal — an offence-creation pattern fired.
        detection: Provenance for the machine signals.
        ofcom_nexus: Machine signals tying this clause to the OSA/Ofcom machinery.
        review_status: Human-review lifecycle state.
        insertion_type: Human judgement — how it could enter the s.216A regime.
        activation_path: Human judgement — the downstream statutory route.
        anchored_harm: Human judgement — the specific harm it could anchor.
        assessment: The human-verified three-filter rubric (None until reviewed).
        triage_score: Derived from machine signals; orders the review queue.
        priority_score: Derived from the human assessment; ranks the finished artefact (None until reviewed).
    """

    provision_id: str
    clause_num: str
    heading: str
    text: str
    creates_or_amends_offence: bool
    detection: DetectionEvidence
    ofcom_nexus: frozenset[OfcomNexusSignal] = frozenset()
    review_status: ReviewStatus = ReviewStatus.PENDING
    insertion_type: InsertionType | None = None
    activation_path: ActivationPath | None = None
    anchored_harm: HarmDomain | None = None
    assessment: ReachabilityAssessment | None = None
    triage_score: float = 0.0
    priority_score: float | None = None


@dataclass
class TrackedBill:
    """A live bill with its bill-level signals and its candidate provisions.

    Attributes:
        bill_id: Parliament Bills API billId.
        short_title: Bill short title.
        long_title: Bill long title.
        carrier_family: Which carrier family the bill belongs to (admissibility signal).
        stage: Current parliamentary stage — recorded as an attribute, never a filter.
        last_update: The bill's own last-updated date from the API.
        snapshot_date: When this record was captured (anchors week-on-week diffs).
        amends_acts: Acts the bill amends, harvested from the text (carrier-classification input).
        provisions: The scored candidate provisions nested under this bill.
    """

    bill_id: int
    short_title: str
    long_title: str
    carrier_family: CarrierFamily
    stage: str
    last_update: date
    snapshot_date: date
    amends_acts: list[str] = field(default_factory=list)
    provisions: list[CandidateProvision] = field(default_factory=list)


# --- Scoring configuration ------------------------------------------------------------

@dataclass
class ScoringWeights:
    """Weights for the two derived scores. Defaults are neutral starting points to tune.

    The priority weights are where the audience split lives: a DSIT/legal preset raises
    facilitation_fit and individual_uk_harm (reachability); a ControlAI/advocacy preset
    raises prevalence and severity (tractability). Same data, two rankings — express each
    as a separate ScoringWeights instance rather than branching in code.

    Attributes:
        facilitation_fit: Priority weight on the facilitation filter.
        individual_uk_harm: Priority weight on the UK-individual-harm filter.
        prevalence: Priority weight on prevalence.
        severity: Priority weight on severity.
        offence_signal: Triage weight when an offence-creation pattern fired.
        per_nexus_signal: Triage weight per distinct Ofcom-nexus signal.
        carrier_fit: Triage weight when the bill sits in a recognised carrier family.
    """

    facilitation_fit: float = 1.0
    individual_uk_harm: float = 1.0
    prevalence: float = 1.0
    severity: float = 1.0
    offence_signal: float = 2.0
    per_nexus_signal: float = 1.0
    carrier_fit: float = 1.0