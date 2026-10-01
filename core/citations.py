"""
Citation checker: every record id the LLM writes (e.g. [D00123], L00045) must exist in the data.

check_citations(text, valid_ids, mode)
    mode="drop"  -> remove unknown ids and append a warning line (default, used in the UI)
    mode="flag"  -> keep them but mark as  D99999 (unverified)
    mode="strip" -> remove ALL ids silently (for customer-facing emails)
Returns (clean_text, report) where report = {"cited": [...], "valid": [...], "invalid": [...]}.
The report is computed BEFORE cleaning, so evaluation measures what the model really produced.
"""
import re


def _pattern(prefixes):
    prefixes = sorted({p for p in prefixes if p}, key=len, reverse=True)
    if not prefixes:
        return None
    return re.compile(r"\b(?:%s)\d{3,}\b" % "|".join(map(re.escape, prefixes)))


def prefixes_of(ids) -> set[str]:
    out = set()
    for i in ids:
        m = re.match(r"[A-Za-z]+", str(i))
        if m:
            out.add(m.group(0))
    return out


def _tidy(text: str) -> str:
    text = re.sub(r"\[\s*[,;\s]*\]", "", text)
    text = re.sub(r"\[\s*[,;]\s*", "[", text)
    text = re.sub(r"\s*[,;]\s*\]", "]", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r" +([.,;:])", r"\1", text).strip()


def check_citations(text: str, valid_ids, mode: str = "drop", prefixes=None):
    valid = {str(v) for v in valid_ids}
    pat = _pattern(prefixes if prefixes is not None else prefixes_of(valid))
    if not text or pat is None:
        return text, {"cited": [], "valid": [], "invalid": []}
    cited = list(dict.fromkeys(pat.findall(text)))
    invalid = [c for c in cited if c not in valid]
    report = {"cited": cited, "valid": [c for c in cited if c in valid], "invalid": invalid}
    if mode == "strip":
        return _tidy(pat.sub("", text)), report
    if not invalid:
        return text, report
    bad = set(invalid)
    if mode == "flag":
        text = pat.sub(lambda m: f"{m.group(0)} (unverified)" if m.group(0) in bad else m.group(0), text)
    else:
        text = _tidy(pat.sub(lambda m: "" if m.group(0) in bad else m.group(0), text))
    return text + f"\n\n_Removed {len(invalid)} citation(s) not found in the data: {', '.join(invalid)}_" if mode == "drop" else text, report
