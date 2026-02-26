"""Configuration — Supports both Anthropic and OpenAI backends."""

import os
from pathlib import Path

# Check which API key is available
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if ANTHROPIC_API_KEY:
                LLM_PROVIDER = "anthropic"
elif OPENAI_API_KEY:
                LLM_PROVIDER = "openai"
else:
                LLM_PROVIDER = None

# Model settings
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"
OPENAI_MODEL = "gpt-4o"

DATA_DIR = Path(__file__).parent / "data"
