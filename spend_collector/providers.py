"""Curated catalog of agent-spend providers, so a policy can name a provider
instead of re-deriving its endpoint. Agent spend is more than LLM tokens: paid
tool / data APIs and payment rails are providers too.

base_urls and unit costs are CURATED DEFAULTS -- verify against each provider's
docs and override in your policy. Nothing here is authoritative billing data.
"""
from __future__ import annotations

# LLM providers. OpenAI-compatible chat/completions unless `usage` says otherwise;
# route at the gateway as /<name>/... (base_url is the provider root, no /v1).
_LLM = {
    "openai":     {"base_url": "https://api.openai.com",             "api_key_env": "OPENAI_API_KEY",      "usage": "openai"},
    "openrouter": {"base_url": "https://openrouter.ai/api",          "api_key_env": "OPENROUTER_API_KEY",  "usage": "openai"},
    "groq":       {"base_url": "https://api.groq.com/openai",        "api_key_env": "GROQ_API_KEY",        "usage": "openai"},
    "together":   {"base_url": "https://api.together.xyz",           "api_key_env": "TOGETHER_API_KEY",    "usage": "openai"},
    "fireworks":  {"base_url": "https://api.fireworks.ai/inference", "api_key_env": "FIREWORKS_API_KEY",   "usage": "openai"},
    "deepinfra":  {"base_url": "https://api.deepinfra.com/v1/openai","api_key_env": "DEEPINFRA_API_KEY",   "usage": "openai"},
    "deepseek":   {"base_url": "https://api.deepseek.com",           "api_key_env": "DEEPSEEK_API_KEY",    "usage": "openai"},
    "xai":        {"base_url": "https://api.x.ai",                   "api_key_env": "XAI_API_KEY",         "usage": "openai"},
    "mistral":    {"base_url": "https://api.mistral.ai",             "api_key_env": "MISTRAL_API_KEY",     "usage": "openai"},
    "perplexity": {"base_url": "https://api.perplexity.ai",          "api_key_env": "PERPLEXITY_API_KEY",  "usage": "openai"},
    "moonshot":   {"base_url": "https://api.moonshot.cn/v1",         "api_key_env": "MOONSHOT_API_KEY",    "usage": "openai"},
    "dashscope":  {"base_url": "https://dashscope.aliyuncs.com/compatible-mode", "api_key_env": "DASHSCOPE_API_KEY", "usage": "openai"},
    "zhipu":      {"base_url": "https://open.bigmodel.cn/api/paas/v4","api_key_env": "ZHIPU_API_KEY",      "usage": "openai"},
    "ollama":     {"base_url": "http://localhost:11434/v1",          "api_key_env": "OLLAMA_API_KEY",      "usage": "openai"},
    "vllm":       {"base_url": "http://localhost:8000/v1",           "api_key_env": "VLLM_API_KEY",        "usage": "openai"},
    # OpenAI-compatible endpoint (returns OpenAI-shape usage)
    "gemini":     {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "api_key_env": "GEMINI_API_KEY", "usage": "openai"},
    # native (non-OpenAI) response shapes
    "anthropic":  {"base_url": "https://api.anthropic.com",          "api_key_env": "ANTHROPIC_API_KEY",   "usage": "anthropic"},
    "cohere":     {"base_url": "https://api.cohere.com",             "api_key_env": "COHERE_API_KEY",      "usage": "cohere"},
}

# Paid tool / data APIs an agent spends on. Priced per call/unit (no token usage),
# so record via a gateway `target` with an `amount` (unit_cost is a rough USD/call
# default -- set your own from your plan). rail groups them in the ledger.
_TOOLS = {
    "tavily":      {"base_url": "https://api.tavily.com",         "api_key_env": "TAVILY_API_KEY",      "unit_cost": 0.008,  "rail": "api"},
    "serper":      {"base_url": "https://google.serper.dev",      "api_key_env": "SERPER_API_KEY",      "unit_cost": 0.001,  "rail": "api"},
    "exa":         {"base_url": "https://api.exa.ai",             "api_key_env": "EXA_API_KEY",         "unit_cost": 0.005,  "rail": "api"},
    "brave":       {"base_url": "https://api.search.brave.com",   "api_key_env": "BRAVE_API_KEY",       "unit_cost": 0.005,  "rail": "api"},
    "firecrawl":   {"base_url": "https://api.firecrawl.dev",      "api_key_env": "FIRECRAWL_API_KEY",   "unit_cost": 0.002,  "rail": "api"},
    "scrapingbee": {"base_url": "https://app.scrapingbee.com/api","api_key_env": "SCRAPINGBEE_API_KEY", "unit_cost": 0.002,  "rail": "api"},
    "apify":       {"base_url": "https://api.apify.com",          "api_key_env": "APIFY_TOKEN",         "unit_cost": 0.01,   "rail": "api"},
    "elevenlabs":  {"base_url": "https://api.elevenlabs.io",      "api_key_env": "ELEVENLABS_API_KEY",  "unit_cost": 0.03,   "rail": "api"},
    "deepgram":    {"base_url": "https://api.deepgram.com",       "api_key_env": "DEEPGRAM_API_KEY",    "unit_cost": 0.0043, "rail": "api"},
    "replicate":   {"base_url": "https://api.replicate.com",      "api_key_env": "REPLICATE_API_TOKEN", "unit_cost": 0.01,   "rail": "api"},
    "fal":         {"base_url": "https://fal.run",                "api_key_env": "FAL_KEY",             "unit_cost": 0.01,   "rail": "api"},
    "e2b":         {"base_url": "https://api.e2b.dev",            "api_key_env": "E2B_API_KEY",         "unit_cost": 0.01,   "rail": "api"},
}

# Payment rails / facilitators. Captured by ingestion (pull), not the gateway.
_PAYMENT = {
    "stripe":   {"rail": "card",     "ingest": "pull-stripe"},
    "x402":     {"rail": "api_x402", "ingest": "pull-x402"},
    "skyfire":  {"rail": "api_x402", "note": "agent payment network"},
    "coinbase": {"rail": "api_x402", "note": "Commerce / x402 facilitator"},
}

KNOWN_PROVIDERS = {**_LLM, **_TOOLS, **_PAYMENT}


def llm_provider(name: str) -> dict | None:
    """Catalog entry for a known LLM provider, or None."""
    entry = _LLM.get(name)
    return dict(entry) if entry else None


def usage_tokens(data: dict) -> tuple[int, int]:
    """(input, output) tokens across LLM response shapes: OpenAI
    (prompt_/completion_tokens), Anthropic (input_/output_tokens), Gemini
    (usageMetadata.*TokenCount), Cohere (meta.billed_units). (0, 0) if none.
    """
    if not isinstance(data, dict):
        return 0, 0
    u = (data.get("usage") or data.get("usageMetadata")
         or (data.get("meta") or {}).get("billed_units") or {})
    if not isinstance(u, dict):
        return 0, 0
    inp = u.get("prompt_tokens") or u.get("input_tokens") or u.get("promptTokenCount") or 0
    out = u.get("completion_tokens") or u.get("output_tokens") or u.get("candidatesTokenCount") or 0
    return int(inp or 0), int(out or 0)
