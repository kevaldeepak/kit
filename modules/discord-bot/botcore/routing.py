"""Route-down tier picker: Haiku for trivial acks, Sonnet default, big model
for clearly heavy work. Pure functions, no I/O."""
import re

# Auto-routing is route-DOWN biased: default a conversational turn to the CHEAP
# model and let it escalate UP only when the turn actually needs it (the inverse
# of the old route-up design, which defaulted to the big model). Three tiers — a
# wrong-cheap is fixed by the escalation hatch, so misroutes degrade to "one
# wasted cheap pass", never a bad answer:
#   • HAIKU  — zero-reasoning turns: pure acks/pleasantries, greetings, liveness
#              pings. Trusted to nail "thanks" / "you alive?".
#   • SONNET — THE DEFAULT for ordinary conversation (questions, chat, light
#              help). Self-escalates to the big model mid-turn if it hits real
#              work (editing files, multi-step builds, deep reasoning).
#   • BIG    — only when the message clearly signals heavy work up front
#              (BIG_WORK_RE) or via escalation from Sonnet/Haiku.

# Tier HAIKU building blocks: acks, greetings, liveness pings. The full regex
# allows one-or-more acks, and an optional greeting optionally followed by a
# liveness phrase ("hi you aliev?" → greeting + typo'd liveness, still trivial).
_ACK = (r"(thanks?( a lot| man| mate| bro)?|thank you|ty|cheers|"
        r"nice( one| work)?( man| mate)?|cool|ok(ay)?|kk|great|perfect|sweet|"
        r"sounds good|got it|gotcha|noted|np|lol|lmao|haha+|"
        r"good( job| stuff| work| night| morning| evening)?|"
        r"love it|awesome|amazing|legend|brilliant|ace|cheers)")
_GREET = (r"(hi+|hey+|hello+|yo+|sup|wassup|howdy|hiya|heya|gm|gn|morning|"
          r"evening|afternoon)")
_LIVE = (r"((are |r )?(you|u) ?(still )?(there|alive|aliev|awake|up|online|live|ok|good)"
         r"|still (there|alive|up|awake|online))")
_SEP = r"[\s,.!?]*"
HAIKU_RE = re.compile(
    r"^(?:"
    rf"{_ACK}(?:{_SEP}{_ACK})*"                 # one or more acks in a row
    rf"|(?:{_GREET}{_SEP})?(?:{_LIVE})?"        # greeting and/or liveness ping
    r")"
    r"[\s,.!?🙂😄😅🎉👍🙏❤️🔥👋]*$", re.I)

# Signals that a turn is heavy enough to START on the big model rather than
# default to Sonnet and pay an escalation round-trip. Deliberately tight — a miss
# just means the turn starts on Sonnet and self-escalates, which is cheap; an
# over-match just means it starts big (no quality loss, only a bit more cost).
BIG_WORK_RE = re.compile(
    r"\b(implement|refactor|rewrite|debug|diagnos|root[- ]?cause|troubleshoot|"
    r"build (a|the|me|out|us)|rebuild|scaffold|"
    r"write\b[\w ]{0,20}(script|code|function|class|program|module|"
    r"test|cli|bot|app|api|service|endpoint|component)|"
    r"research|investigate|deep[- ]?dive|audit|"
    r"architect|design (a|the|an)|migrat|optimi[sz]e|benchmark|"
    r"deploy|provision|configure|set ?up|install|"
    r"review (the |this |my )?(code|pr|diff|repo|changes|branch)|"
    r"plan (a|the|out|this)|step[- ]by[- ]step|"
    r"create (a|the|an) (script|app|service|endpoint|component|feature|page))\b",
    re.I)

def default_tier(body, has_attachment=False):
    """Route-DOWN model tier for a conversational turn:
       'haiku' → trivial acks/greetings/liveness pings,
       None    → clearly heavy work (start on the big model directly),
       'fast'  → DEFAULT (Sonnet); the escalation hatch upgrades it mid-turn if
                 it turns out to need real work.
    This is the inverse of the old route-UP heuristic: instead of defaulting to
    the big model and going cheap only on a pattern match, we default to Sonnet
    and go big only on a pattern match — far cheaper for ordinary conversation."""
    b = (body or "").strip()
    if not b:
        return "fast" if has_attachment else None
    if HAIKU_RE.match(b) and not has_attachment:
        return "haiku"
    if BIG_WORK_RE.search(b) or len(b) > 600:
        return None
    return "fast"
