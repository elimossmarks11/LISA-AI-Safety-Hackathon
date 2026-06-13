# UK-AI-Regulation-Tracker
A repository containing code to track developments in the UK's AI regulation. 

## Project Context

The UK is yet to define a legislative regime to comprehensively regulate AI (McGurk and Tomlinson, 54). The Conservative government's 2023 White Paper, _A Pro-Innovation Approach to AI Regulation_ endorsed a 'principles-based', 'context-specific', 'outcomes-focussed' redirection of sectoral laws and regulations in order to govern this rapidly rising technology (DSIT 2023). The current Labour government has in many ways continued this approach, with the Parliamentary Under-Secretary of State in the DSIT stating in November 2025 that 'AI is already regulated in the UK and we regulate on a context-specific approach' (House of Lords, November 2025).

However, a recent legislative development suggests new means by which the UK can regulate AI. S.216A of the Online Safety Act 2023 permits the Secretary of State to amend any provision of the Act to minimise the risks presented by: 

- illegal AI-generated content;
- the use of AI services for the commission or facilitation of priority offences (Online Safety Act 2023, s 216A)

This new power opens the door significantly to a new AI-related offences being placed under the list of priority offences. Crimes which have the potential to be committed with the use of AI (such as cybercrime and biocrime) could feasibly be placed on this list of priority offences, rendering the use of AI services to achieve them prosecutable. 

This repository serves as a tool for interested members of the public to track the passage of current legislation through parliament that could be amended by this new power of the Secretary of State. Bills are flagged based on keyword matching based on the presence of words related to crime and whether they could feasibly be related to AI (in line with the OSA's stipulation that crimes will be added to the priority offences only if these traits are present). 

## How to run this project

### Prerequisites

The environment is defined in `environment.yml` (Python 3.11, conda-forge). Create and activate it with conda or mamba:

```bash
conda env create -f environment.yml
conda activate tracker
```

### Step 1 — Build the local bill cache

`fetch_bills.py` snapshots the Akoma Ntoso (AKN) XML of every live, current-session bill into a local cache, one file per bill (`data/raw_xml/{billId}.xml`):

```bash
python fetch_bills.py
```

It is polite to the source (a delay between fetches) and idempotent: on re-run it reuses a cached file unless the bill has been re-published since, so you can run it repeatedly to keep the snapshot fresh. A `SOURCE.txt` attribution file (Open Parliament Licence v3.0) is written alongside the cache.

Useful flags:

- `--limit N` — only attempt the first N live bills; good for a quick smoke test before a full run.
- `--force` — re-fetch every live bill regardless of cache state.
- `--cache-dir PATH` — write the snapshot somewhere other than `data/raw_xml`.
- `-v` / `--verbose` — debug-level logging (per-page and per-fetch detail).

The command prints a summary (saved / refreshed / already-current / no-XML / failed) and exits non-zero if any document failed to download.

### Step 2 — Serve the flagged shortlist

`serve.py` scans the cache, scores every bill, and exposes the flagged ones over an HTTP API with auto-generated Swagger docs:

```bash
python serve.py
# or, with autoreload during development:
uvicorn serve:app --reload
```

It serves on `http://127.0.0.1:8000` by default. Open `/docs` for the interactive Swagger UI, or call `GET /flagged_bills` directly (optionally with `?min_triage=<float>` to set a floor on a bill's top clause score). The cache is read once at startup and held in memory, so after re-running `fetch_bills.py` you need to restart the service to pick up changes. Point it at a different cache without editing code via the `S216_CACHE_DIR` environment variable:

```bash
S216_CACHE_DIR=/path/to/cache python serve.py
```

## Bibliography

Brendan McGurk and Joe Tomlinson, _Artificial Intelligence and Public Law_ (2025)

Department for Science, Innovation, and Technology (DSIT), AI Regulation: A Pro-Innovation Approach (CP 815, 2023) (AI White Paper)

House of Lords (November 2025) Artificial Intelligence Legislation, Hansard, HL Deb 17 November. Available at: https://hansard.parliament.uk/Lords/2025-11-17/debates/BD4C0FAB-9CFF-445F-9A9D-83FFEACEFD70/ArtificialIntelligenceLegislation (Accessed: 9 June 2026).

Online Safety Act 2023, s 216A (inserted by Crime and Policing Act 2026, s 248(2)).

