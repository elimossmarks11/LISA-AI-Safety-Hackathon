You are an assistant that is helping me build a tracker of UK AI legislation for an AI safety hackathon. 

The pipeline will locate relevant legislation as it's primary task, potentially incorporating a RAG pipeline afterwards. This is to be decided.

You will be tasked with conducting preliminary research and writing code. All code for .py files should follow the standards laid out below. Code for test .ipynb files doesn't need to follow this full workflow (e.g. logging, ruff formatted etc) - it should be as concise as possible for quick tests, which is what I will be using .ipynb files for.

## Adding new libraries 

Do not introduce a new library without consulting me. I will push you to create scripts generate empirical evidence (benchmark, comparison, or evaluation metric) demonstrating it outperforms the current approach. 

## Coding Standards

### Python Development
- Use Python 3.10+
- Format with Ruff (`ruff format .`)
- Lint with Ruff (`ruff check .`)
- Always include type hints
- Use Google-style docstrings
- Follow PEP 8 with max line length 120
- Each function has to do one thing only
- When several functions have the same arguments, create a dataclass called `RetrievalConfig`
- Don't duplicate code. Ensure only one instance of an object is created and shared across the program
- Unless lazy imports are required, all libraries should only be imported once, at the top of the .ipynb or .py file.

### No Hardcoded Values
- Never hardcode thresholds, paths, or magic numbers inline
- Extract constants to module-level `UPPER_SNAKE_CASE` variables with a documenting comment
- If a value truly cannot be parameterised, it must have a comment explaining **why** it is hardcoded

### Logging
- Use the `logging` module for all library/module code
- Use `click.echo()` **only** inside CLI entry points (Click command functions)
- Never use bare `print()` for operational output

### Naming Conventions
- Functions/variables: snake_case
- Classes: PascalCase
- Constants: UPPER_SNAKE_CASE
- Files: snake_case.py

## Module Boundaries

Each module has a single responsibility. Do not leak logic across boundaries.

**Rules:**
- Functions must be idempotent — calling them twice with the same input produces the same output
-  shared configuration flows downward from `pipeline.py` via arguments or a `RetrievalConfig` dataclass
- If `utils.py` grows unwieldy, split by concern (e.g., `extraction.py` for PDF parsing/chunking). Verify before doing this.

## Data Directory Conventions

To be filled in as we build.

## Notebooks vs Scripts

- **Notebooks** (`.ipynb`) are sandboxes for experimentation and exploration only
- **Production code** lives exclusively in `.py` modules
- Never put pipeline logic in a notebook; extract proven approaches into the appropriate module

## Workflow Guidelines

### Planning
- Never commit or edit files without securing confirmation first

### Error Handling
- Use try-except with proper logging (`logging` module)
- Provide clear error messages
- Don't silently ignore exceptions

## Preferences

- Provide concise, focused responses
- Show code examples when helpful
- Explain the "why" behind changes
- Prefer editing existing files over creating new ones
- Only create documentation when explicitly requested