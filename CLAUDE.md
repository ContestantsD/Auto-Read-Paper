# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Auto-Read-Paper fetches newly-announced arXiv papers daily, runs a keyword pre-filter + multi-agent (Reader + Reviewer) LLM rerank, generates localized three-section deep-read summaries, and delivers an HTML digest by email. Runs entirely on GitHub Actions at zero cost.

## Commands

```bash
# Run the application
uv run src/auto_read_paper/main.py

# Run tests (excludes slow tests by default)
uv run pytest

# Run all tests including slow ones
uv run pytest -m ""

# Run a single test
uv run pytest tests/test_utils.py::TestGlobMatch -v

# Install/sync dependencies
uv sync
```

No linter or formatter is configured.

## Architecture

The app follows a linear pipeline orchestrated by `Executor` (`src/auto_read_paper/executor.py`):

1. **Retrieve** — pulls newly-announced arXiv papers in the configured categories via the RSS feed; arXiv-native author affiliations are fetched in the same pass
2. **Keyword pre-filter** — drops papers whose title/abstract doesn't match any configured keyword (before any LLM call)
3. **Rerank** — either `reader_reviewer` (default: Reader extracts per-paper structured notes, Reviewer batch-ranks them) or `keyword_llm` (per-paper LLM scoring)
4. **History merge** — today's scored papers merge with the past-N-days unsent pool from `state/score_history.json`
5. **Deep read** — the Top-N are sent back to the LLM for a three-section localized summary (`[CORE]` / `[INNOVATION]` / `[VALUE]`) and, if needed, a translated title
6. **Render + send email** — HTML template rendered and sent via SMTP; papers are only marked sent after SMTP succeeds

### Plugin Systems

**Retrievers** (`src/auto_read_paper/retriever/`): Register via `@register_retriever` decorator, discovered by `get_retriever_cls()`. Each retriever implements `_retrieve_raw_papers()` and `convert_to_paper()`.

**Rerankers** (`src/auto_read_paper/reranker/`): Register via `@register_reranker` decorator, discovered by `get_reranker_cls()`. Two implementations: `keyword_llm` (per-paper LLM scoring) and `reader_reviewer` (two-agent pipeline, default).

### Configuration

Uses Hydra + OmegaConf. Config is composed from `config/base.yaml` (defaults) + `config/custom.yaml` (user overrides). Environment variables are interpolated via `${oc.env:VAR_NAME,default}` syntax. Entry point uses `@hydra.main`.

### Data Classes

`Paper` in `src/auto_read_paper/protocol.py`. `Paper` has LLM-powered methods (`generate_tldr`, `generate_title_zh`, `generate_affiliations`) that call the OpenAI API directly.

## Testing

Tests marked `@pytest.mark.slow` require heavy dependencies (e.g., sentence-transformers model download) and are skipped locally by default (`addopts = "-m 'not slow'"` in pyproject.toml). All other tests run with pure Python stubs (no Docker containers needed).

```bash
# Run tests (excludes slow tests)
uv run pytest

# Run all tests including slow ones
uv run pytest -m ""

# Run with coverage
uv run pytest --cov=src/auto_read_paper --cov-report=term-missing
```

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available skills: `/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`, `/design-consultation`, `/design-shotgun`, `/design-html`, `/review`, `/ship`, `/land-and-deploy`, `/canary`, `/benchmark`, `/browse`, `/connect-chrome`, `/qa`, `/qa-only`, `/design-review`, `/setup-browser-cookies`, `/setup-deploy`, `/retro`, `/investigate`, `/document-release`, `/codex`, `/cso`, `/autoplan`, `/plan-devex-review`, `/devex-review`, `/careful`, `/freeze`, `/guard`, `/unfreeze`, `/gstack-upgrade`, `/learn`.

If gstack skills aren't working, run `cd .claude/skills/gstack && ./setup` to build the binary and register skills.

## Git Workflow

- PRs should target the `dev` branch, not `main`
- Current development branch: `dev`
