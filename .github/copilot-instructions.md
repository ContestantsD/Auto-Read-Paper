# Copilot Instructions

## Project Overview

Auto-Read-Paper fetches newly-announced arXiv papers daily, runs a keyword pre-filter + multi-agent (Reader + Reviewer) LLM rerank, generates localized three-section deep-read summaries, and delivers an HTML digest by email. Runs entirely on GitHub Actions at zero cost.

## Commands

```bash
# Install/sync dependencies
uv sync

# Run the application
uv run src/auto_read_paper/main.py

# Run tests (excludes slow tests by default)
uv run pytest

# Run all tests including slow ones
uv run pytest -m ""

# Run a single test
uv run pytest tests/test_utils.py::TestGlobMatch -v
```

No linter or formatter is configured.

## Architecture

The app is a linear pipeline orchestrated by `Executor` (`src/auto_read_paper/executor.py`):

1. **Retrieve** → arXiv RSS + per-batch Atom API call for native author affiliations
2. **Keyword pre-filter** → drop papers whose title/abstract doesn't match any configured keyword
3. **Rerank** → `reader_reviewer` (default, two-agent) or `keyword_llm` (per-paper LLM scoring)
4. **History merge** → merge today's scored papers with the past-N-days unsent pool (`state/score_history.json`)
5. **Deep read** → Top-N sent to LLM for localized `[CORE]` / `[INNOVATION]` / `[VALUE]` summary + optional translated title
6. **Render + send email** → HTML template via SMTP; papers marked sent only after SMTP succeeds

### Plugin Systems

**Retrievers** (`src/auto_read_paper/retriever/`): Register via `@register_retriever("name")` decorator on a `BaseRetriever` subclass. Each retriever implements `_retrieve_raw_papers()` and `convert_to_paper()`. Discovered at runtime via `get_retriever_cls(name)` from a module-level `registered_retrievers` dict.

**Rerankers** (`src/auto_read_paper/reranker/`): Register via `@register_reranker("name")` decorator on a `BaseReranker` subclass. Two implementations: `keyword_llm` (per-paper LLM scoring) and `reader_reviewer` (Reader extracts structured notes, Reviewer batch-ranks). Discovered via `get_reranker_cls(name)`.

When adding a new retriever or reranker, follow the existing pattern: create a new file, subclass the base, apply the registration decorator, and implement the abstract methods.

### Configuration

Uses Hydra + OmegaConf. Config composes from `config/base.yaml` (defaults) + `config/custom.yaml` (user overrides). The composition order is defined in `config/default.yaml`. Environment variables are interpolated via `${oc.env:VAR_NAME,default}` syntax. Entry point uses `@hydra.main(config_name="default")`.

### Data Classes

`Paper` in `src/auto_read_paper/protocol.py`. `Paper` has LLM-powered methods (`generate_tldr`, `generate_title_zh`, `generate_affiliations`) that call the OpenAI API directly with `tiktoken`-based token truncation.

## Testing Conventions

- Tests use **pytest monkeypatch + `SimpleNamespace`** for stubs — not `unittest.mock`.
- A session-scoped Hydra config in `tests/conftest.py` is deep-copied per test via the `config` fixture.
- Canned response factories live in `tests/canned_responses.py` (e.g., `make_stub_openai_client()`).
- Tests marked `@pytest.mark.slow` require heavy dependencies (model downloads) and are excluded by default (`addopts = "-m 'not slow'"` in pyproject.toml).
- Monkeypatching targets the module-level import path.

## Coding Conventions

- **Logging:** `loguru.logger` throughout — never `print()` or stdlib `logging`.
- **Type hints:** Modern Python 3.10+ syntax (`list[Paper]`, `str | None`).
- **Constants:** Module-level `UPPER_SNAKE_CASE`.
- **Private methods:** Prefixed with `_` (e.g., `_retrieve_raw_papers`).
- **Error handling:** Graceful degradation with try/except and fallback logic; log warnings rather than raising.
- **Config injection:** All major components receive `DictConfig` at init and store it as `self.config`.

## Git Workflow

- PRs should target the **`dev`** branch, not `main`.
