"""What the tool has learned about one user, inferred from their requests and behaviour (never asked).

Each browser sends an anonymous id (`client`). Its profile is built from that client's events in the usage
log: the industry and language of their requests, the volumes they mention or set, the use cases they
correct to, how often they test 3 times, and their sovereignty choices. The profile tailors their next
results: the concrete test is written for their industry and language, an unclear use case follows their
usual one, and costs are shown at their usual scale. Users can see the profile and delete it.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from typing import Any

from . import usage

MIN_REQUESTS = 2          # below this, nothing is tailored
PRIOR_SHARE = 0.7         # a use case is "usual" when at least this share of the user's tasks have it...
MIN_USE_CASE_TASKS = 4    # ...and the user has settled at least this many tasks
UNSURE_BELOW = 0.6        # the guessed use case is unclear below this probability
DOMAIN_SHARE = 0.4        # the leading industry's share of all industry evidence
DOMAIN_LABELS = {
    "retail_ecommerce": "retail and e-commerce", "logistics": "logistics", "banking_finance": "banking and finance",
    "telecom": "telecom", "healthcare": "healthcare", "government": "government", "manufacturing": "manufacturing",
    "hr": "HR", "legal": "legal", "education": "education",
}
_ARABIC = re.compile(r"[\u0600-\u06FF]")
_LETTER = re.compile(r"[^\W\d_]")
_PERIODS = {
    "day": "day", "week": "week", "month": "month", "year": "year",
    "اليوم": "day", "يوم": "day", "يوميا": "day", "يومياً": "day", "الأسبوع": "week", "أسبوع": "week", "أسبوعيا": "week",
    "الشهر": "month", "شهر": "month", "شهريا": "month", "شهرياً": "month", "السنة": "year", "سنة": "year", "سنويا": "year",
}
_VOLUME = re.compile(
    r"(\d[\d,.]*)\s*(k|thousand|million|m|ألف|مليون)?\s+(?:[^\d\s]+\s+){0,4}?"
    r"(?:per|a|each|every|/|في|كل)?\s*(" + "|".join(sorted(_PERIODS, key=len, reverse=True)) + r")\b", re.I)
_PER_MONTH = {"day": 22, "week": 4.3, "month": 1, "year": 1 / 12}  # working days in a month
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٬", "0123456789,")


_CLIENT = re.compile(r"[A-Za-z0-9-]{8,64}")


def valid_client(value: str) -> str:
    """The browser's anonymous id, or "" if it isn't one (so arbitrary text never reaches the log)."""
    return value if value and _CLIENT.fullmatch(value) else ""


def arabic_share(text: str) -> float:
    letters = _LETTER.findall(text)
    return sum(1 for ch in letters if _ARABIC.match(ch)) / len(letters) if letters else 0.0


def stated_monthly_volume(text: str) -> int | None:
    """A volume the user mentions ("500 invoices a month", "2k emails per day", "3000 شحنة في الشهر"), per month."""
    match = _VOLUME.search(text.translate(_ARABIC_DIGITS))
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    scale = {"k": 1e3, "thousand": 1e3, "ألف": 1e3, "m": 1e6, "million": 1e6, "مليون": 1e6}.get((match.group(2) or "").lower(), 1)
    return int(number * scale * _PER_MONTH[_PERIODS[match.group(3).lower()]])


def build_profile(client: str, events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    events = [e for e in (events if events is not None else usage.load_events()) if e.get("client") == client]
    requests = [e for e in events if e["kind"] == "request" and e.get("via") == "start"]
    comparisons = [e for e in events if e["kind"] == "comparison_done"]
    changes = [e for e in events if e["kind"] == "use_case_changed"]
    profile: dict[str, Any] = {"client": client, "requests": len(requests), "comparisons": len(comparisons)}
    if not requests:
        return profile

    # Industry: add up each request's industry probabilities (a shipping complaint is part logistics, part
    # retail), ignoring "general"; the leader must hold a clear share of what the requests point to.
    scores: Counter[str] = Counter()
    for e in requests:
        given = e.get("domain_scores") or ({e["domain"]: 1.0} if e.get("domain") else {})
        scores.update({d: p for d, p in given.items() if d != "general"})
    if scores:
        domain, score = scores.most_common(1)[0]
        if score / sum(scores.values()) >= DOMAIN_SHARE and score >= 0.5:
            profile["domain"] = domain
    arabic = sum(1 for e in requests if (e.get("arabic_share") or 0) >= 0.3)
    profile["language"] = "Arabic" if arabic / len(requests) >= 0.5 else "English"

    # Use cases they settle on: a correction counts double, since it is the user's own choice.
    settled = Counter(c.get("use_case") for c in comparisons if not c.get("rerun"))
    for change in changes:
        settled[change.get("from_use_case")] -= 1
        settled[change.get("to_use_case")] += 2
    total = sum(v for v in settled.values() if v > 0)
    if total >= MIN_USE_CASE_TASKS:
        use_case, count = max(settled.items(), key=lambda item: item[1])
        if count / total >= PRIOR_SHARE:
            profile["usual_use_case"] = use_case
    profile["use_case_corrections"] = len(changes)

    volumes = [e["monthly_volume"] for e in events if e.get("monthly_volume")]
    volumes += [v for e in events if e["kind"] == "volumes_saved" for v in e.get("volumes", [])]
    if volumes:
        profile["monthly_volume"] = int(statistics.median(volumes))
    reliable = [c for c in comparisons if (c.get("trials") or 1) > 1]
    profile["prefers_reliability"] = len(comparisons) >= 2 and len(reliable) / len(comparisons) >= 0.5
    latest = requests[-1]
    profile["country"] = latest.get("country")
    profile["sovereignty"] = latest.get("sovereignty")
    profile["tailoring"] = len(requests) >= MIN_REQUESTS
    return profile


def design_context(profile: dict[str, Any]) -> str:
    """A note for the task writer, so the concrete test looks like this user's work."""
    if not profile.get("tailoring"):
        return ""
    parts = []
    if profile.get("domain"):
        parts.append(f"works in {DOMAIN_LABELS.get(profile['domain'], profile['domain'])}")
    if profile.get("language") == "Arabic":
        parts.append("writes in Arabic, so write the scenario and the instruction in Arabic")
    if profile.get("country"):
        parts.append(f"is based in country code {profile['country']}")
    return ("The user " + "; ".join(parts) + ". Unless the request says otherwise, set the scenario in that context.") if parts else ""


def use_case_prior(profile: dict[str, Any], guessed: str, probabilities: dict[str, float]) -> tuple[str, str | None]:
    """Follow the user's usual use case when their words leave it unclear. Returns (use case, reason)."""
    usual = profile.get("usual_use_case")
    if not profile.get("tailoring") or not usual or usual == guessed:
        return guessed, None
    if probabilities.get(guessed, 0.0) >= UNSURE_BELOW:
        return guessed, None
    return usual, "Your words left the use case unclear, so we used the one you usually choose."


def forget(client: str) -> int:
    """Delete every event of this client from the usage log. Returns how many were removed."""
    removed = 0
    for path in usage.USAGE_DIR.glob("events-*.jsonl"):
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [line for line in lines if line.strip() and json.loads(line).get("client") != client]
        removed += len(lines) - len(kept)
        path.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")
    return removed
