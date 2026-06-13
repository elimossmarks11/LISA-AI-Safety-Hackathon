## Contributing Guideline

Thank you for your interest in this project! The most helpful work to extend this project would be to incorporate an LLM integration to pick up on the semantic meaning behind the bills rather than purely relying on keyword matching.

## How this project works

The system is a three-stage pipeline — fetch, model, detect/serve — built around the question of which live bills could feed the s.216A regime.

### `fetch_bills.py` — the fetcher

This is the data-acquisition stage, a `click` CLI that gets the raw bill text onto disk. It talks to two different Parliament hosts because they behave differently:

- The **Bills API** (`bills-api.parliament.uk`) is an ungated public API, used to page through the full bill index and to look up each bill's publications. The script discovers the current parliamentary session by taking the largest session id it sees (rather than hardcoding one, which would break across prorogations), then filters to bills that are in that session and still live — not withdrawn, defeated, or already enacted.

The result is a clean, attributed, idempotent local snapshot that the rest of the pipeline reads.

### `models.py` — the domain model

This is a dependency-free (standard-library only) set of dataclasses and enums describing what a "carrier candidate" is and how it's assessed. It encodes a two-stage workflow:

- **Machine-detected signals**, set automatically by the detection step: whether a clause creates or amends an offence, which Ofcom/OSA-nexus signals fired (amends the Online Safety Act 2023, amends the Communications Act 2003, mentions Ofcom, mentions a priority offence, references Schedule 7), the literal terms that matched, and a coarse `CarrierFamily` bucket for the bill. These feed a `triage_score` that orders the review queue.
- **Human-verified judgement**, set at review: a three-filter `ReachabilityAssessment` — whether an AI service could facilitate the offence, whether the harm falls on a UK individual, and the harm's prevalence and severity — plus how a provision could enter the regime (`InsertionType`) and the downstream statutory route to switch on a duty (`ActivationPath`). These feed a `priority_score`.

`CandidateProvision` (one clause) and `TrackedBill` (a bill plus its provisions) are the core records. `ScoringWeights` keeps the two scores tunable, and is designed so the same data can be ranked differently for different audiences — a legal/regulatory reading that weights legal reachability, versus an advocacy reading that weights how common and severe the harm is — by swapping in a different weights instance rather than branching the code.

### `serve.py` — the API

This is the presentation stage: a FastAPI app that turns the scored cache into a queryable shortlist. At startup it loads and scores every AKN file in the cache once and holds the flagged bills in application state. The `GET /flagged_bills` endpoint returns each bill that has at least one flagged clause, highest-scoring first, with a generated plain-English `reason` (naming the offence-creating clauses and/or the clauses that touch the OSA/Ofcom machinery), a per-clause breakdown, and triage scores; `min_triage` filters by a bill's top score. The response models are Pydantic schemas, so the whole thing is documented automatically at `/docs`.

The actual detection logic (`extract_bill` and the set of non-bill filenames to skip) is imported from a separate `smoke_test` module — an interim seam the code's own comments flag as temporary, with the intended end state being a shared `detection.py` that both the service and a smoke test import. That module isn't in this repo yet (see the run-section note above), so the detection step is the one piece you'll need to supply to get the API running end-to-end.

