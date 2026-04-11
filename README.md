# Multi-Agent Orchestration Framework

A study in multi-agent system design — a 4-agent decision intelligence framework that explores how LLMs can coordinate to perform retrieval, semantic analysis, content generation, and web interaction over real-world unstructured data. Built to understand multi-agent coordination patterns, LLM reasoning over heterogeneous documents, and the tradeoffs between different LLM providers.

The job market is used as the test domain because it provides a rich, noisy, real-world data source: unstructured postings, structured candidate profiles, semantic matching, and dynamic web environments to navigate.

## Why This Project

I built this to answer questions I had about multi-agent systems:

- How do you design specialized agents that share state through a pipeline orchestrator?
- Can an LLM reliably score the semantic match between an unstructured document and a structured profile?
- How much does the choice of LLM provider (Anthropic vs OpenAI) actually affect reasoning quality and latency?
- What does an LLM-agnostic abstraction layer look like in practice?
- How do you build a browser-interaction agent that adapts to dynamic, JS-heavy web pages?

This repo is the result of working through those questions end-to-end.

## The 4 Agents

| Agent | Role | Key Tech |
|---|---|---|
| **Retrieval Agent** | Discovers and aggregates source documents from multiple endpoints | Requests, BeautifulSoup |
| **Analysis Agent** | LLM-powered semantic scoring of unstructured documents against a structured profile (0–100 confidence) | LangChain, RAG, Pydantic |
| **Generation Agent** | Rewrites and reorders content to mirror the target document's keyword distribution | LLM prompting, structured output |
| **Web Interaction Agent** | Navigates dynamic, JS-rendered web environments using a browser automation runtime | Playwright |

All four agents communicate through a shared **pipeline orchestrator** that handles state passing, error recovery, and step-by-step execution.

## Key Design Decisions

- **LLM-agnostic abstraction layer** — Hot-swap between Anthropic (Claude Sonnet 4) and OpenAI (GPT-4o) via a single env var. The agents never see the underlying provider.
- **Structured outputs everywhere** — All LLM calls return Pydantic-validated JSON, making agent output deterministic enough to chain.
- **Provider benchmarking** — Latency and reasoning quality measured across providers to inform agent design tradeoffs.
- **Decoupled storage** — SQLite persistence layer with deduplication so agents are idempotent across runs.
- **Graceful degradation** — Each agent can fail independently without crashing the pipeline.

## Architecture

```
multi-agent-web-automation/
├── main.py                      # CLI orchestrator
├── config.py                    # LLM provider abstraction
│
├── agents/
│   ├── workflow_engine.py       # Pipeline orchestrator (state passing, error recovery)
│   ├── job_searcher.py          # Retrieval Agent — multi-source document aggregation
│   ├── job_parser.py            # Document parsing (BeautifulSoup + Playwright fallback)
│   ├── tailoring_agent.py       # Analysis Agent — semantic scoring + content generation
│   ├── auto_applier.py          # Web Interaction Agent — Playwright-driven navigation
│   ├── profile_loader.py        # Structured profile parsing
│   ├── tracker.py               # SQLite persistence + deduplication
│   ├── discovery_agent.py       # Free API source integration
│   ├── batch_processor.py       # Batch execution mode
│   └── form_validator.py        # DOM verification
│
├── models/
│   └── __init__.py              # Pydantic data contracts (Profile, Document, Analysis)
│
├── utils/
│   ├── llm.py                   # Unified LLM client (provider-agnostic)
│   ├── pdf_generator.py         # Markdown → DOCX/PDF conversion
│   └── logging_config.py        # Structured logging
│
├── data/
│   ├── my_resume.md             # Test profile (input)
│   ├── profile.json             # Parsed profile cache
│   ├── preferences.json         # Configuration defaults
│   └── applications.db          # SQLite state
│
└── output/
    └── {document_id}/           # Per-document agent outputs
        ├── resume.md
        ├── cover_letter.md
        └── fit_analysis.md
```

## Pipeline Flow

```
Retrieval Agent → fetches documents from source endpoints
       │
       ▼
Pipeline Orchestrator → deduplication, state initialization
       │
       ▼
Document Parser → BeautifulSoup or Playwright (for JS-rendered pages)
       │
       ▼
Analysis Agent → LLM semantic scoring (0–100) via RAG
       │
       ├── Score < threshold → terminate pipeline
       │
       ▼
Generation Agent → tailored content output
       │
       ▼
Web Interaction Agent → optional navigation step (configurable)
       │
       ▼
Persistence Layer → SQLite + filesystem
```

## Quick Start

### Prerequisites

- Python 3.12+
- Anthropic or OpenAI API key
- Playwright with Chromium

### Installation

```bash
git clone https://github.com/AdityaModi7/multi-agent-web-automation.git
cd multi-agent-web-automation
pip install -r requirements.txt
playwright install chromium
```

### Configuration

```bash
# Pick your LLM provider
export ANTHROPIC_API_KEY=sk-ant-...
# or
export OPENAI_API_KEY=sk-...
```

Add a test profile to `data/my_resume.md` and adjust `data/preferences.json` to control pipeline behavior.

## Usage

```bash
# Run the entire pipeline (dry-run, no side effects)
python main.py run

# Run on a single source document
python main.py apply --url https://example.com/document


# Inspect persisted state
python main.py dashboard
python main.py list
```

## What I Learned

- **Multi-agent coordination is mostly about state contracts.** Once agents agree on Pydantic schemas, the orchestrator becomes trivial. Most of the complexity is in defining good interfaces.
- **LLM-agnostic code is harder than it sounds.** Anthropic and OpenAI differ in how they handle structured outputs, retries, and rate limits — abstracting them required custom JSON repair logic.
- **Semantic scoring is surprisingly stable across providers.** Latency and cost differ significantly, but the actual 0–100 scores were within 5 points across Claude Sonnet 4 and GPT-4o.
- **Browser automation agents need defensive design.** Real-world web pages are noisy, slow, and inconsistent. The Web Interaction Agent has retries, popup dismissal, and adaptive selectors at every step.
- **Error recovery matters more than error prevention.** Designing each agent to fail independently and resume gracefully made the system far more robust than trying to prevent failures upfront.


