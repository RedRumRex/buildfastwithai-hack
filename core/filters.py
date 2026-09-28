"""
Plain-English filters for the action queue, e.g.
    "skip anyone contacted in the last 2 weeks, FinTech and Healthcare only, over $20k, top 15"

parse() tries the LLM first (when a key is configured) and ALWAYS validates its output against the
allowed values, so the LLM can only fill in a fixed set of filter fields – it can't invent an
industry, a stage or a crazy number. If the LLM is unavailable, times out or returns junk, the
rule-based parser is used instead, so the demo never breaks.

Filter fields (all optional):
    exclude_contacted_days  int   – hide leads contacted within the last N days (1-365)
    industries              list  – keep only these industries
    stages                  list  – keep only leads whose top open deal is in these stages
    min_deal                int   – keep only leads whose largest open deal is >= this ($)
    hide_stale              bool  – hide stale / unreachable leads
    top_n                   int   – how many leads to show (1-50)
"""
import re

from . import llm

FIELDS = ["exclude_contacted_days", "industries", "stages", "min_deal", "hide_stale", "top_n"]
DEFAULT_STAGES = ["New", "Qualified", "Demo Scheduled", "Proposal Sent", "Negotiation"]

_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
              "thirty": 30}
_UNIT_DAYS = {"day": 1, "week": 7, "fortnight": 14, "month": 30}
_EXCLUDE_WORDS = ["exclude", "skip", "not contacted", "without", "remove", "except", "haven't", "havent",
                  "hasn't", "hasnt", "ignore", "drop", "hide", "no one", "nobody", "not been contacted",
                  "not touched", "untouched", "leave out", "avoid"]
_MONEY_UNITS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "l": 1e5, "lakh": 1e5,
                "lakhs": 1e5, "lac": 1e5, "cr": 1e7, "crore": 1e7}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _num(tok: str) -> int | None:
    tok = tok.strip().lower()
    return int(tok) if tok.isdigit() else _NUM_WORDS.get(tok)


# ---------------------------------------------------------------------------
# Rule-based parser (fallback, no AI)
# ---------------------------------------------------------------------------
def parse_rules(text: str, industries: list[str], stages: list[str] | None = None) -> dict:
    stages = stages or DEFAULT_STAGES
    t = " " + text.lower().replace(",", " , ") + " "
    t_nocomma = re.sub(r"(\d),(\d)", r"\1\2", text.lower())
    f = {}

    # 1. "skip anyone contacted in the last 2 weeks" / "not contacted this week"
    if any(w in t for w in _EXCLUDE_WORDS):
        num = r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty)"
        m = re.search(rf"(?:last|past|within|in|over|for)\s+(?:the\s+)?(?:last\s+|past\s+)?{num}\s*(day|week|fortnight|month)s?\b", t) \
            or re.search(rf"\b{num}\s*(day|week|fortnight|month)s?\b", t)
        if m and _num(m.group(1)):
            f["exclude_contacted_days"] = _num(m.group(1)) * _UNIT_DAYS[m.group(2)]
        elif re.search(r"\b(this|last|past)\s+week\b", t):
            f["exclude_contacted_days"] = 7
        elif re.search(r"\b(this|last|past)\s+month\b", t):
            f["exclude_contacted_days"] = 30
        elif re.search(r"\b(today|yesterday)\b", t):
            f["exclude_contacted_days"] = 1 if "today" in t else 2

    # 2. industries (tolerant to case, spaces and hyphens: "ecommerce", "e-commerce", "health care")
    nt = _norm(text)
    inds = [i for i in industries if _norm(i) and _norm(i) in nt]
    if inds:
        f["industries"] = inds

    # 3. deal stages ("negotiation only", "late stage", "proposal sent")
    st_ = []
    if re.search(r"\blate[- ]stage", t):
        st_ += [s for s in ("Proposal Sent", "Negotiation") if s in stages]
    for s in stages:
        key = s.lower()
        if key in t or (s == "Negotiation" and re.search(r"\bnegotiat", t)) or (s == "Proposal Sent" and re.search(r"\bproposals?\b", t)):
            if s == "New" and not re.search(r"\bnew (deals?|stage|leads?)\b", t):
                continue
            if s == "Qualified" and re.search(r"\b(un|not |dis)qualified", t):
                continue
            st_.append(s)
    if st_:
        f["stages"] = list(dict.fromkeys(st_))

    # 4. minimum deal size: "over $20k", "at least 50,000", "deals above 5 lakh" (not "over 2 weeks")
    units = "|".join(sorted(_MONEY_UNITS, key=len, reverse=True))
    m = re.search(rf"(?:over|above|more than|greater than|bigger than|at least|min(?:imum)?(?: deal)?(?: of)?|>=?|≥)\s*"
                  rf"(?:usd|\$|rs\.?|₹|inr)?\s*(\d+(?:\.\d+)?)\s*({units})?\b(?!\s*(?:days?|weeks?|months?|years?|employees|people|leads?))",
                  t_nocomma)
    if m:
        f["min_deal"] = int(float(m.group(1)) * _MONEY_UNITS.get(m.group(2) or "", 1))

    # 5. stale leads
    if "stale" in t or "unreachable" in t:
        f["hide_stale"] = not re.search(r"\b(include|show|with|keep|incl\.?)\b[^,]*\b(stale|unreachable)", t)

    # 6. how many
    m = re.search(r"\btop\s+(\d+)\b", t) or re.search(r"\b(?:show|list|give me)\s+(?:me\s+)?(\d+)\b", t) \
        or re.search(r"\b(\d+)\s+(?:leads|people|contacts|accounts)\b", t)
    if m:
        f["top_n"] = int(m.group(1))
    return validate(f, industries, stages)


# ---------------------------------------------------------------------------
# LLM parser (validated)
# ---------------------------------------------------------------------------
def parse_llm(text: str, industries: list[str], stages: list[str] | None = None) -> dict:
    stages = stages or DEFAULT_STAGES
    out = llm.chat_json(
        "You convert a sales manager's instruction into filters for a ranked list of sales leads. "
        "Only use these keys and leave out anything the instruction does not ask for:\n"
        '  "exclude_contacted_days": integer – hide leads contacted within the last N days (e.g. "last 2 weeks" -> 14)\n'
        f'  "industries": list chosen ONLY from {industries}\n'
        f'  "stages": list chosen ONLY from {stages} (stage of the lead\'s biggest open deal; "late stage" = Proposal Sent + Negotiation)\n'
        '  "min_deal": integer US dollars – minimum size of the lead\'s largest open deal ("$20k" -> 20000)\n'
        '  "hide_stale": boolean – true to hide stale/unreachable leads, false to include them\n'
        '  "top_n": integer 1-50 – how many leads to show\n'
        "Return {} if nothing applies.",
        text)
    if not isinstance(out, dict):
        raise ValueError("LLM did not return a JSON object")
    return validate(out, industries, stages)


def validate(raw: dict, industries: list[str], stages: list[str] | None = None) -> dict:
    """Keep only known fields with sane values. Anything else is silently dropped."""
    stages = stages or DEFAULT_STAGES
    f = {}

    def as_int(v, lo, hi):
        try:
            v = int(float(v))
        except (TypeError, ValueError):
            return None
        return max(lo, min(hi, v))

    if raw.get("exclude_contacted_days") not in (None, "", 0):
        v = as_int(raw["exclude_contacted_days"], 1, 365)
        if v:
            f["exclude_contacted_days"] = v
    for key, allowed in (("industries", industries), ("stages", stages)):
        vals = raw.get(key)
        if isinstance(vals, str):
            vals = [vals]
        if isinstance(vals, list) and vals:
            lut = {_norm(a): a for a in allowed}
            keep = [lut[_norm(str(x))] for x in vals if _norm(str(x)) in lut]
            if keep:
                f[key] = list(dict.fromkeys(keep))
    if raw.get("min_deal") not in (None, "", 0):
        v = as_int(raw["min_deal"], 0, 100_000_000)
        if v:
            f["min_deal"] = v
    if isinstance(raw.get("hide_stale"), bool):
        f["hide_stale"] = raw["hide_stale"]
    if raw.get("top_n") not in (None, "", 0):
        v = as_int(raw["top_n"], 1, 50)
        if v:
            f["top_n"] = v
    return f


def parse(text: str, industries: list[str], stages: list[str] | None = None, use_llm: bool | None = None) -> tuple[dict, str]:
    """Return (filters, engine) where engine is 'llm' or 'rules'."""
    text = (text or "").strip()
    if not text:
        return {}, "rules"
    if use_llm is None:
        use_llm = llm.available()
    if use_llm:
        try:
            f = parse_llm(text, industries, stages)
            if f:
                return f, "llm"
        except Exception:
            pass
    return parse_rules(text, industries, stages), "rules"
