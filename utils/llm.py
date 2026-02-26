"""Unified LLM client — wraps Anthropic and OpenAI behind one interface."""

import os
import json
import re


def get_client():
    """Get the active LLM provider name and validate setup."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if anthropic_key:
        return "anthropic"
    elif openai_key:
        return "openai"
    else:
        raise RuntimeError(
            "No API key found. Set one of:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-...\n"
            "  export OPENAI_API_KEY=sk-..."
        )


def call_llm(system_prompt: str, user_message: str, max_tokens: int = 2000) -> str:
    """Call the LLM and return the text response.

    Works with either Anthropic or OpenAI depending on which key is set.
    """
    provider = get_client()

    if provider == "anthropic":
        from anthropic import Anthropic
        client = Anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return message.content[0].text.strip()

    elif provider == "openai":
        from openai import OpenAI
        client = OpenAI()
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
        return response.choices[0].message.content.strip()


def call_llm_json(system_prompt: str, user_message: str, max_tokens: int = 2000) -> dict:
    """Call the LLM and parse the response as JSON.

    Handles markdown code fences that models sometimes add.
    """
    response_text = call_llm(system_prompt, user_message, max_tokens)

    # Strip markdown code fences
    response_text = re.sub(r"^```json?\s*", "", response_text)
    response_text = re.sub(r"\s*```$", "", response_text)

    return json.loads(response_text)
