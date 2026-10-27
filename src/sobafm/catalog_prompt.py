"""Small phrase vocabulary, not a general musical-language interpreter."""

import re
import unicodedata
from dataclasses import dataclass

from sobafm.catalog import CatalogInputError, Feature, Query, parse_query

PHRASES: dict[str, Feature] = {
    "solo piano": Feature.SOLO_PIANO,
    "piano": Feature.PIANO,
    "quiet": Feature.LOW_ENERGY,
    "relaxed": Feature.LOW_ENERGY,
    "gentle": Feature.LOW_ENERGY,
    "slow": Feature.LOW_ENERGY,
    "laid back": Feature.LOW_ENERGY,
    "low energy": Feature.LOW_ENERGY,
    "upbeat": Feature.HIGH_ENERGY,
    "energetic": Feature.HIGH_ENERGY,
    "fast": Feature.HIGH_ENERGY,
    "high energy": Feature.HIGH_ENERGY,
    "vocals": Feature.VOCALS,
    "singing": Feature.VOCALS,
    "singer": Feature.VOCALS,
    "sung vocals": Feature.VOCALS,
    "sung": Feature.VOCALS,
    "drums": Feature.DRUMS,
    "acoustic": Feature.ACOUSTIC,
    "electronic": Feature.ELECTRONIC,
    "folk": Feature.FOLK,
    "jazz": Feature.JAZZ,
    "swing": Feature.SWING,
    "funk": Feature.FUNK,
    "strong bass groove": Feature.BASS_GROOVE,
    "bass groove": Feature.BASS_GROOVE,
    "indie pop": Feature.INDIE_POP,
    "catchy": Feature.CATCHY,
    "cheerful": Feature.CHEERFUL,
    "hip hop": Feature.HIPHOP,
    "lo fi": Feature.LOFI,
    "lofi": Feature.LOFI,
    "warm": Feature.WARM,
    "spacious": Feature.SPACIOUS,
    "ambient": Feature.AMBIENT,
    "synthwave": Feature.SYNTHWAVE,
    "dreamy": Feature.DREAMY,
    "driving": Feature.DRIVING,
    "orchestral": Feature.ORCHESTRAL,
    "dramatic": Feature.DRAMATIC,
    "heavy metal": Feature.METAL,
    "metal": Feature.METAL,
    "guitar": Feature.GUITAR,
    "guitars": Feature.GUITAR,
    "blues": Feature.BLUES,
    "french": Feature.FRENCH,
    "rapped vocals": Feature.RAP,
    "rap": Feature.RAP,
    "old school": Feature.OLD_SCHOOL,
}
_ORDERED_PHRASES = sorted((tuple(p.split()), f) for p, f in PHRASES.items())
_ORDERED_PHRASES.sort(key=lambda item: -len(item[0]))
_CONTEXT = frozenset(
    [
        "a",
        "an",
        "the",
        "music",
        "songs",
        "song",
        "tracks",
        "track",
        "with",
        "for",
        "reading",
        "cooking",
        "studying",
        "study",
        "winding",
        "down",
        "sunday",
        "morning",
        "late",
        "night",
        "drive",
        "workout",
        "scene",
        "afternoon",
        "to",
        "some",
        "and",
        "feel",
        "in",
        "of",
        "play",
        "please",
        "on",
    ]
)
_NEGATIONS = frozenset({"no", "without", "not", "exclude", "except"})


@dataclass(frozen=True)
class Interpretation:
    query: Query | None
    unsupported_terms: tuple[str, ...]
    problem: str | None


def _phrase(tokens: list[str], position: int) -> tuple[Feature, int] | None:
    for phrase, feature in _ORDERED_PHRASES:
        if tuple(tokens[position : position + len(phrase)]) == phrase:
            return feature, len(phrase)
    return None


def normalize_prompt(prompt: str) -> Interpretation:
    """Report unsupported vocabulary and refuse ambiguous hard constraints.

    Free-form positive descriptors may yield a partial interpretation. Unknown
    negation, identity or duration syntax yields no executable query.
    """
    if not 1 <= len(prompt.strip()) <= 200 or len(prompt) > 200:
        raise CatalogInputError("prompt must contain 1 to 200 characters")
    if any(unicodedata.category(c).startswith("C") for c in prompt):
        raise CatalogInputError("prompt contains control characters")
    normalized = unicodedata.normalize("NFKC", prompt.strip()).casefold()
    lookup = re.fullmatch(r"play (.+?) by (.+?)[.!]?", normalized)
    if lookup:
        return Interpretation(parse_query({"title": lookup[1], "artist": lookup[2]}), (), None)
    if re.search(
        r"\b(avoid|never|excluding|must|required|dislike|hate|non|sans|free|less|cannot|dont)\b"
        r"|\b(don|doesn|didn|isn|aren|wasn|weren|can|won|wouldn|shouldn|mustn)['’]t\b"
        r"|\b(do not|anything but|other than|rather than|instead of)\b",
        normalized,
    ):
        return Interpretation(None, (), "unsupported exclusion or mandatory-constraint syntax")
    if re.search(r"\b(by|recording of|version of|cover of|original|exact)\b", normalized):
        return Interpretation(
            None, (), "unsupported identity syntax; use explicit query constraints"
        )
    query_data: dict[str, object] = {}
    duration_pattern = r"\b(at least|at most|under|over|exactly) (\d+) (seconds?|minutes?|hours?)\b"
    durations = list(re.finditer(duration_pattern, normalized))
    for match in durations:
        multiplier = (
            3600 if match[3].startswith("hour") else 60 if match[3].startswith("minute") else 1
        )
        seconds = int(match[2]) * multiplier
        minimum = seconds + (match[1] == "over")
        maximum = seconds - (match[1] == "under")
        fields = (
            ("min_seconds", "max_seconds")
            if match[1] == "exactly"
            else (("min_seconds",) if match[1] in ("at least", "over") else ("max_seconds",))
        )
        for field in fields:
            if field in query_data:
                return Interpretation(None, (), "duplicate duration constraints")
            query_data[field] = minimum if field == "min_seconds" else maximum
    normalized = re.sub(duration_pattern, " ", normalized)
    if re.search(r"\d|\b(seconds?|minutes?|hours?|shorter|longer|less|more)\b", normalized):
        return Interpretation(
            None, (), "unsupported duration syntax; use explicit query constraints"
        )
    tokens = re.findall(r"[^\W_]+", normalized.replace("-", " "))
    preferences: dict[Feature, bool] = {}
    required: dict[Feature, bool] = {}
    unsupported: list[str] = []
    position = 0
    while position < len(tokens):
        token = tokens[position]
        if token == "only":
            if tokens[position + 1 : position + 2] != ["piano"]:
                return Interpretation(None, tuple(unsupported), "unsupported only constraint")
            if tokens[position + 2 : position + 3] in (["or"], ["and"]):
                return Interpretation(None, tuple(unsupported), "ambiguous only constraint")
            if required.get(Feature.SOLO_PIANO) is False:
                return Interpretation(None, tuple(unsupported), "contradictory hard constraints")
            required[Feature.SOLO_PIANO] = True
            position += 2
            continue
        if token in _NEGATIONS:
            value = False
            position += 1
            while True:
                found = _phrase(tokens, position)
                if found is None:
                    return Interpretation(None, tuple(unsupported), "unsupported hard constraint")
                feature, count = found
                if feature in required and required[feature] != value:
                    return Interpretation(
                        None, tuple(unsupported), "contradictory hard constraints"
                    )
                required[feature] = value
                position += count
                if position < len(tokens) and tokens[position] == "and":
                    return Interpretation(
                        None, tuple(unsupported), "ambiguous hard-constraint conjunction"
                    )
                if position < len(tokens) and tokens[position] == "or":
                    position += 1
                    continue
                break
            continue
        if token == "instrumental":
            if required.get(Feature.VOCALS) is True:
                return Interpretation(None, tuple(unsupported), "contradictory hard constraints")
            required[Feature.VOCALS] = False
            position += 1
            continue
        found = _phrase(tokens, position)
        if found:
            feature, count = found
            preferences[feature] = True
            position += count
        else:
            if token not in _CONTEXT:
                unsupported.append(token)
            position += 1
    query_data["preferences"] = [{"feature": f, "value": v} for f, v in preferences.items()]
    query_data["required"] = [{"feature": f, "value": v} for f, v in required.items()]
    try:
        query = parse_query(query_data)
    except CatalogInputError as exc:
        return Interpretation(None, tuple(dict.fromkeys(unsupported)), str(exc))
    return Interpretation(query, tuple(dict.fromkeys(unsupported)), None)
