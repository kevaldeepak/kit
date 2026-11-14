"""Model registry, tier resolution, footer labels and helper-call flags.

Single source of truth for every model id the bot uses. Bot-agnostic: no
Discord or path dependencies, no bot globals."""


# ──── MODEL REGISTRY: single source of truth for every model id ─────
# Keys are what /model pins and run_streaming accept. "fallback" is what the CLI
# drops to (--fallback-model) AND what a usage-credits rejection retries on.
# Haiku 5.5: the API serves it fine; the CLI just prints an `unrecognized_model`
# warning on stderr (its model list predates it). 4.5 kept as the fallback.
MODELS = {
    "opus":    {"id": "claude-opus-5-5",   "fallback": "claude-opus-4-8", "label": "Opus 5.5"},
    "opus4.8": {"id": "claude-opus-4-8",   "fallback": None,              "label": "Opus 4.8"},
    "fable5.2": {"id": "claude-fable-5-2", "fallback": "claude-opus-5-5",              "label": "Fable 5.2"},
    "fable":   {"id": "claude-fable-5-1",  "fallback": "claude-opus-5-5",              "label": "Fable 5.1"},
    "fable5":  {"id": "claude-fable-5",    "fallback": "claude-opus-5-5",              "label": "Fable 5"},
    "sonnet":  {"id": "claude-sonnet-5-5", "fallback": None,              "label": "Sonnet 5.5"},
    "haiku":   {"id": "claude-haiku-5-5",  "fallback": "claude-haiku-4-5", "label": "Haiku 5.5"},
}
# Override (bot.conf BIG_MODEL=sonnet|opus|opus4.8): on a smaller plan, make
# Sonnet the "big" brain so auto-routing never burns Opus quota;
# explicit /model pins still reach any model.
from botcore.config import conf as _conf
DEFAULT_BIG = _conf.get("BIG_MODEL", "").strip().lower() or "opus"
if DEFAULT_BIG not in MODELS:
    DEFAULT_BIG = "opus"
# Auto-routing tier tokens → registry key. "big" is the default/escalation
# target. Fable is NEVER auto-picked: reachable only via an explicit /model pin.
TIERS = {"fast": "sonnet", "haiku": "haiku", "big": DEFAULT_BIG}


def resolve_model(token):
    """Tier token ("fast"|"haiku"|"big") or registry key → registry entry."""
    key = token if token in MODELS else TIERS.get(token, DEFAULT_BIG)
    return MODELS[key]


FAST_MODEL = MODELS["sonnet"]["id"]
# Haiku is reachable ONLY via an explicit /model haiku pin or per-message
# override — never via the auto-router (triage/heuristic only pick Sonnet/big).
HAIKU_MODEL = MODELS["haiku"]["id"]
# Lightweight helper calls (title/triage/dispatch/summary) need NO tools, but a
# plain `claude -p` still loads every connected MCP server's tool schemas into
# context, and a large connector can push that past the context limit. Pin these
# calls to an empty MCP config: ~12.5k tokens, immune to connector state.
HELPER_MCP_FLAGS = ("--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}')


# Human-facing model names for the per-reply usage footer. Keyed by the internal
# model token used in run_turn / run_streaming (tier token or registry key).
# Used only as a fallback when the stream didn't report a concrete model id.
BIG_LABEL = MODELS[DEFAULT_BIG]["label"]
MODEL_LABELS = {**{k: v["label"] for k, v in MODELS.items()},
                **{t: MODELS[k]["label"] for t, k in TIERS.items()}}

# The model we ASK for (the "big" tier token) isn't always the model that
# ANSWERS: we ask for Opus 5.5, and if it's unavailable/overloaded the CLI's
# --fallback-model silently serves the turn on Opus 4.8 instead. The footer must
# name the model that actually ran, read from each stream's assistant event
# (message.model). Map the concrete model id to a friendly label here.
MODEL_ID_LABELS = {
    "claude-opus-5-5": "Opus 5.5",
    "claude-opus-4-8": "Opus 4.8",
    "claude-fable-5-2": "Fable 5.2",
    "claude-fable-5-1": "Fable 5.1",
    "claude-fable-5": "Fable 5",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-haiku-5-5": "Haiku 5.5",
    "claude-haiku-4-5": "Haiku 4.5",
}


def model_label(actual_id, fallback_token):
    """Friendly name for the model that actually answered. Prefer the concrete
    id reported by the stream; fall back to the requested-token label.

    The stream often reports a DATE-SUFFIXED id (e.g. 'claude-haiku-4-5-20251001'),
    so match by longest known prefix rather than exact equality — otherwise the
    footer shows the raw id."""
    if actual_id:
        if actual_id in MODEL_ID_LABELS:
            return MODEL_ID_LABELS[actual_id]
        for known, label in sorted(MODEL_ID_LABELS.items(), key=lambda kv: -len(kv[0])):
            if actual_id.startswith(known):
                return label
        # Genuinely unknown id — show it raw rather than mislabel.
        return actual_id
    return MODEL_LABELS.get(fallback_token, fallback_token or "?")


def _fmt_tok(n):
    """Compact token count: 950 → '950', 12345 → '12.3k'."""
    n = int(n or 0)
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def usage_footer(model_token, usage, cost=0.0, escalated=False, actual_id=None, note=None):
    """Discord small-text line ('-#' …) naming the model that ACTUALLY answered
    (Fable vs the Opus fallback) and the turn's token spend. Returns '' when
    there's no usage to report (e.g. errors that never produced a result event),
    so we don't tack on a '0 tokens' line."""
    u = usage or {}
    tin = int(u.get("input_tokens", 0) or 0)
    tout = int(u.get("output_tokens", 0) or 0)
    cache_r = int(u.get("cache_read_input_tokens", 0) or 0)
    cache_w = int(u.get("cache_creation_input_tokens", 0) or 0)
    total = tin + tout + cache_r + cache_w
    if total == 0:
        return ""
    label = model_label(actual_id, model_token)
    if escalated:
        label += " (escalated)"
    if note:
        label += f" ({note})"
    parts = [f"{_fmt_tok(tin)} in", f"{_fmt_tok(tout)} out"]
    if cache_r:
        parts.append(f"{_fmt_tok(cache_r)} cached")
    if cache_w:
        parts.append(f"{_fmt_tok(cache_w)} cache-write")
    line = f"-# 🧠 {label} · {_fmt_tok(total)} tokens ({' · '.join(parts)})"
    if cost:
        line += f" · ${cost:.4f}"
    return line
