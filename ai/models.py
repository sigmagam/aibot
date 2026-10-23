"""
Model catalog, taken from the 9.todict.tech dashboard (provider screenshots).

Only four providers are exposed here (everything else on the dashboard is
intentionally left out):
    tokenharbor  -> the default model used for plain messages
    antigravity
    nvidia
    cloudflare

MODEL_PRIORITY  -> default auto-flow order tried first for plain messages
    (typed directly, mention, or reply-to-bot — no /ai involved):
    1. tokenharbor/deepseek-v4.1-flash:free

MODEL_CATALOG   -> the same four providers grouped for the /ai picker; it
    doubles as the random fallback pool when MODEL_PRIORITY fails.
"""
from __future__ import annotations

import random

# Default auto-flow order, tried first for plain (non-/ai) messages.
MODEL_PRIORITY: list[str] = [
    "tokenharbor/deepseek-v4.1-flash:free",  # the one default model
]

# Models known to support "thinking" / reasoning streaming. Used to decide
# whether the reasoning stream gets shown as an expandable blockquote.
THINKING_MODELS: set[str] = {
    "ag/claude-opus-4-6-thinking",
    "ag/claude-sonnet-4-6",
    "ag/gemini-pro-agent",
    "ag/gemini-3-flash-agent",
}

# Display labels for the /ai provider picker.
PROVIDER_LABELS: dict[str, str] = {
    "tokenharbor": "TokenHarbor",
    "antigravity": "Antigravity",
    "nvidia": "NVIDIA NIM",
    "cloudflare": "Cloudflare",
}

# Full catalog (only the four providers this bot exposes).
MODEL_CATALOG: dict[str, list[str]] = {
    "tokenharbor": [
        "tokenharbor/deepseek-v4.1-flash:free",
    ],
    "antigravity": [
        "ag/gemini-3.7-flash-high",
        "ag/gemini-3.7-flash-medium",
        "ag/gemini-3.7-flash-low",
        "ag/gemini-3.6-flash-high",
        "ag/gemini-3.6-flash-medium",
        "ag/gemini-3.6-flash-low",
        "ag/gemini-3.5-flash-high",
        "ag/gemini-3-flash-agent",
        "ag/gemini-3.5-flash-low",
        "ag/gemini-3.5-flash-extra-low",
        "ag/gemini-pro-agent",
        "ag/gemini-3.1-pro-low",
        "ag/claude-sonnet-4-6",
        "ag/claude-opus-4-6-thinking",
        "ag/gpt-oss-120b-medium",
        "ag/gemini-3-flash",
    ],
    "nvidia": [
        "nvidia/minimaxai/minimax-m2.7",
        "nvidia/z-ai/glm-5.2",
        "nvidia/deepseek-ai/deepseek-v4-pro",
        "nvidia/deepseek-ai/deepseek-v4-flash",
        "nvidia/moonshotai/kimi-k2.6",
        "nvidia/moonshotai/kimi-k3",
        "nvidia/minimaxai/minimax-m3",
        "nvidia/poolside/laguna-xs-2.1",
        "nvidia/meta/muse-glimmer-30b",
        "nvidia/nvidia/nemotron-3-ultra-550b-a55b",
    ],
    "cloudflare": [
        "cf/@cf/meta/llama-3.2-1b-instruct",
        "cf/@cf/meta/llama-3.2-3b-instruct",
        "cf/@cf/meta/llama-3.1-8b-instruct-fp8-fast",
        "cf/@cf/meta/llama-3.1-8b-instruct-awq",
        "cf/@cf/mistralai/mistral-small-3.1-24b-instruct",
        "cf/@cf/meta/llama-3.1-70b-instruct-fp8-fast",
        "cf/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "cf/@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
        "cf/@cf/moonshotai/kimi-k2.5",
        "cf/@cf/moonshotai/kimi-k2.6",
        "cf/@cf/zai-org/glm-4.7-flash",
        "cf/@cf/qwen/qwq-32b",
        "cf/@cf/qwen/qwen2.5-coder-32b-instruct",
    ],
}

def all_models() -> list[str]:
    """Flatten every model from every provider into a single list."""
    models: list[str] = []
    for provider_models in MODEL_CATALOG.values():
        models.extend(provider_models)
    return models

def is_thinking_model(model: str) -> bool:
    return model in THINKING_MODELS or model.endswith("-thinking")

def provider_of(model: str) -> str | None:
    """Look up which provider key a given model id belongs to."""
    for provider, models in MODEL_CATALOG.items():
        if model in models:
            return provider
    return None

def pick_random_fallback(exclude: set[str]) -> list[str]:
    """
    Build a shuffled fallback model order (outside MODEL_PRIORITY and
    anything already tried / excluded), so there's always another option
    left if one fails.
    """
    pool = [m for m in all_models() if m not in exclude]
    random.shuffle(pool)
    return pool

def default_candidates() -> list[str]:
    """
    Candidate order for a plain message (typed directly, mention, or
    reply-to-bot — no /ai involved): MODEL_PRIORITY first
    (tokenharbor/deepseek-v4.1-flash:free), then a shuffled fallback across
    the rest of the catalog if it fails.
    """
    rest = pick_random_fallback(exclude=set(MODEL_PRIORITY))
    return [*MODEL_PRIORITY, *rest]

def provider_candidates(provider: str, chosen_model: str) -> list[str]:
    """
    Candidate order for the /ai flow once a provider + model were picked:
    the chosen model first, then the rest of that same provider's models
    as same-provider fallback, then a shuffled fallback across every
    other provider if the whole chosen provider fails.
    """
    provider_models = list(MODEL_CATALOG.get(provider, []))
    same_provider_rest = [m for m in provider_models if m != chosen_model]
    random.shuffle(same_provider_rest)

    other_pool = pick_random_fallback(exclude=set(provider_models))

    return [chosen_model, *same_provider_rest, *other_pool]
