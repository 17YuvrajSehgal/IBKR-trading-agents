"""
Keyword-lexicon news classifier.

Pure-Python phrase matcher that scores a headline on a roughly -5..+5 scale:

    +3..+5  Strong positive  (earnings beat, raised guidance, FDA approval)
    +1..+2  Mild positive    (analyst upgrade, contract win)
       0    Neutral / unknown
    -1..-2  Mild negative    (analyst downgrade, weak guidance)
    -3..-5  Strong negative  (earnings miss, lawsuit, recall, fraud)

Approach
--------
We match against compound phrases (longer phrases scored first) so that
negation lives in the phrase itself ("misses estimates", "fails to beat",
"raised guidance" vs "lowers guidance"). This avoids a separate negation
pass and is good enough for retail headline tagging.

The classifier returns a ``NewsScore`` with the numeric sentiment, the
phrases matched, and an ``is_actionable`` flag that the agent uses to
decide whether to fire an order.

Tune the lexicon for your universe — semiconductor news talks about
"foundry deal" and "node ramp", pharma news talks about "Phase 3" and
"adcom vote". This module is pure data + a small matcher, so swap as
needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Lexicon
# ---------------------------------------------------------------------------
# (phrase, score). Longer phrases first so they out-rank substrings.
# Lower-case everywhere; matched against lower-cased headline.

POSITIVE_PHRASES: list[tuple[str, int]] = [
    # Earnings / guidance — high conviction
    ("beats estimates", 3),
    ("beats expectations", 3),
    ("crushes estimates", 4),
    ("tops estimates", 3),
    ("tops expectations", 3),
    ("smashes estimates", 4),
    ("raises full-year guidance", 4),
    ("raises full year guidance", 4),
    ("raises fy guidance", 4),
    ("raises guidance", 3),
    ("raised guidance", 3),
    ("raises outlook", 3),
    ("raises forecast", 3),
    ("raises q2 forecast", 3),
    ("raises q3 forecast", 3),
    ("raises q4 forecast", 3),
    ("raises q1 forecast", 3),
    ("raises full-year forecast", 4),
    ("record revenue", 2),
    ("record quarter", 2),
    ("record profit", 2),
    ("blowout quarter", 4),
    ("blowout earnings", 4),
    ("earnings beat", 3),
    ("revenue beat", 3),
    ("beats on revenue", 3),
    ("beats on earnings", 3),
    # Capital return
    ("announces share buyback", 3),
    ("announces buyback", 3),
    ("share repurchase program", 2),
    ("raises dividend", 2),
    ("hikes dividend", 2),
    ("increases dividend", 2),
    ("special dividend", 2),
    # Corporate action — positive
    ("acquisition target", 3),
    ("takeover target", 3),
    ("acquires", 1),
    ("strategic review", 1),
    ("explores sale", 2),
    ("strategic partnership", 2),
    ("wins contract", 2),
    ("secures contract", 2),
    ("awarded contract", 2),
    ("multi-year deal", 2),
    ("multi-year contract", 2),
    # Analyst — moderate conviction
    ("upgraded to buy", 2),
    ("upgraded to overweight", 2),
    ("upgrade to buy", 2),
    ("initiated buy", 2),
    ("initiated overweight", 2),
    ("price target raised", 2),
    ("raises price target", 2),
    ("raises pt", 2),
    ("upgraded", 1),
    ("outperform", 1),
    ("strong buy", 2),
    # Regulatory / approvals
    ("fda approves", 4),
    ("fda approval", 4),
    ("ema approves", 3),
    ("approves drug", 3),
    ("approved by fda", 4),
    ("regulatory approval", 3),
    ("phase 3 success", 4),
    ("phase 3 met", 3),
    ("phase 3 primary endpoint", 3),
    # Generic positive
    ("breakthrough", 2),
    ("breakthrough designation", 3),
    ("record high", 1),
    ("all-time high", 1),
    ("expands operations", 1),
    ("launches new product", 1),
]


NEGATIVE_PHRASES: list[tuple[str, int]] = [
    # Earnings / guidance — high conviction
    ("misses estimates", -3),
    ("misses expectations", -3),
    ("misses by", -3),
    ("misses on revenue", -3),
    ("misses on earnings", -3),
    ("falls short of estimates", -3),
    ("fails to beat", -3),
    ("earnings miss", -3),
    ("revenue miss", -3),
    ("guidance below", -3),
    ("guidance disappoints", -3),
    ("cuts full-year guidance", -4),
    ("cuts fy guidance", -4),
    ("cuts guidance", -3),
    ("lowers guidance", -3),
    ("lowered guidance", -3),
    ("slashes guidance", -4),
    ("withdraws guidance", -4),
    ("cuts outlook", -3),
    ("lowers outlook", -3),
    ("cuts forecast", -3),
    ("warns on q2", -3),
    ("warns on q3", -3),
    ("warns on q4", -3),
    ("warns on q1", -3),
    ("profit warning", -4),
    ("revenue warning", -4),
    ("loss widens", -2),
    ("net loss", -1),
    # Regulatory / legal
    ("sec probe", -3),
    ("sec investigation", -3),
    ("doj probe", -3),
    ("doj investigation", -3),
    ("ftc probe", -3),
    ("ftc investigation", -3),
    ("under investigation", -2),
    ("class action lawsuit", -2),
    ("class action", -2),
    ("lawsuit filed", -2),
    ("subpoena", -2),
    ("fraud allegations", -4),
    ("accounting fraud", -5),
    ("accounting irregularities", -4),
    ("restating earnings", -3),
    ("restatement", -3),
    ("delisting risk", -4),
    ("delisting notice", -4),
    ("bankruptcy", -5),
    ("chapter 11", -5),
    ("going concern", -4),
    ("product recall", -3),
    ("massive recall", -4),
    ("recalls product", -3),
    # FDA / regulatory rejection
    ("fda rejects", -4),
    ("fda rejection", -4),
    ("complete response letter", -3),
    ("phase 3 fails", -4),
    ("phase 3 missed", -4),
    ("trial failure", -4),
    ("trial failed", -4),
    ("trial halted", -3),
    ("clinical hold", -3),
    # Analyst — moderate conviction
    ("downgraded to sell", -2),
    ("downgraded to underperform", -2),
    ("downgrade to sell", -2),
    ("price target cut", -2),
    ("lowers price target", -2),
    ("lowers pt", -2),
    ("downgraded", -1),
    ("underperform", -1),
    ("strong sell", -2),
    ("sell rating", -1),
    # People / management
    ("ceo resigns", -2),
    ("ceo steps down", -2),
    ("ceo fired", -3),
    ("cfo resigns", -2),
    ("cfo steps down", -2),
    ("cfo fired", -3),
    ("mass layoffs", -2),
    ("layoffs", -1),
    # Misc strong negatives
    ("data breach", -2),
    ("cyberattack", -2),
    ("ransomware", -2),
    ("hacked", -2),
    ("guidance suspended", -3),
    ("suspends dividend", -3),
    ("cuts dividend", -3),
]


# Provider quality — higher = more reliable, gets a confidence multiplier.
# Codes you'll commonly see from IBKR: BRFG, BRFUPDN, DJ-RT, DJNL, BZ.
PROVIDER_WEIGHTS: dict[str, float] = {
    "DJNL": 1.0,        # Dow Jones Newswire — premium, very fast
    "DJ-RT": 1.0,       # Dow Jones realtime
    "DJ-N": 0.95,       # Dow Jones news
    "RSF-Z": 0.95,      # Reuters
    "BRFG": 0.8,        # Briefing.com — free with trades data, slower
    "BRFUPDN": 0.7,     # Briefing general market column
    "BZ": 0.6,          # Benzinga — included by default but noisy
    "FLY": 0.6,         # The Fly — analyst chatter
}
DEFAULT_PROVIDER_WEIGHT = 0.5  # unknown provider gets this


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class NewsScore:
    """Score for one headline."""

    headline: str
    score: float                       # signed sentiment (negatives boosted by provider weight)
    raw_score: int                     # sum of matched phrase scores before weighting
    provider_code: str
    provider_weight: float
    matched: list[tuple[str, int]] = field(default_factory=list)

    # Convenience
    @property
    def is_actionable(self) -> bool:
        """True if the weighted score is strong enough to warrant a trade."""
        return abs(self.score) >= 2.5

    @property
    def is_positive(self) -> bool:
        return self.score > 0

    @property
    def is_negative(self) -> bool:
        return self.score < 0

    def __repr__(self) -> str:
        sign = "+" if self.score >= 0 else ""
        return (
            f"NewsScore({sign}{self.score:.1f} "
            f"[{self.provider_code}] matched={[m[0] for m in self.matched]})"
        )


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class NewsClassifier:
    """
    Score headlines with a hand-curated phrase lexicon.

    Headlines are lower-cased, then each phrase is searched with whole-word
    boundaries where possible (\\b around the phrase). Matched phrases'
    scores are summed and multiplied by the provider's weight.

    Usage:
        >>> clf = NewsClassifier()
        >>> s = clf.score("AAPL beats estimates, raises Q4 guidance", "DJNL")
        >>> s.is_actionable, s.is_positive
        (True, True)
    """

    def __init__(
        self,
        positive_phrases: list[tuple[str, int]] = None,
        negative_phrases: list[tuple[str, int]] = None,
        provider_weights: dict[str, float] = None,
        action_threshold: float = 2.5,
    ) -> None:
        self.positive = list(positive_phrases or POSITIVE_PHRASES)
        self.negative = list(negative_phrases or NEGATIVE_PHRASES)
        self.provider_weights = dict(provider_weights or PROVIDER_WEIGHTS)
        self.action_threshold = action_threshold

        # Sort longest-first so compound phrases out-match substrings.
        self._sorted_positive = sorted(self.positive, key=lambda p: -len(p[0]))
        self._sorted_negative = sorted(self.negative, key=lambda p: -len(p[0]))

        # Pre-compile each phrase to a regex with word boundaries on alnum edges.
        self._compiled_positive = [
            (re.compile(self._to_pattern(p), re.IGNORECASE), score, p)
            for p, score in self._sorted_positive
        ]
        self._compiled_negative = [
            (re.compile(self._to_pattern(p), re.IGNORECASE), score, p)
            for p, score in self._sorted_negative
        ]

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def score(self, headline: str, provider_code: str = "") -> NewsScore:
        text = headline.lower()
        matched: list[tuple[str, int]] = []
        consumed_spans: list[tuple[int, int]] = []
        raw = 0

        # Match negative phrases first — they include "fails to beat",
        # which contains "beat"; we want the longer negative to win.
        for pattern, score, phrase in self._compiled_negative:
            for m in pattern.finditer(text):
                span = m.span()
                if not self._overlaps_consumed(span, consumed_spans):
                    consumed_spans.append(span)
                    matched.append((phrase, score))
                    raw += score

        for pattern, score, phrase in self._compiled_positive:
            for m in pattern.finditer(text):
                span = m.span()
                if not self._overlaps_consumed(span, consumed_spans):
                    consumed_spans.append(span)
                    matched.append((phrase, score))
                    raw += score

        weight = self.provider_weights.get(provider_code, DEFAULT_PROVIDER_WEIGHT)
        weighted = raw * weight

        return NewsScore(
            headline=headline,
            score=weighted,
            raw_score=raw,
            provider_code=provider_code,
            provider_weight=weight,
            matched=matched,
        )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    @staticmethod
    def _to_pattern(phrase: str) -> str:
        """Word-boundary regex for a phrase. Treats internal spaces as
        flexible whitespace so 'cuts  guidance' still matches 'cuts guidance'."""
        escaped = re.escape(phrase).replace(r"\ ", r"\s+")
        return rf"\b{escaped}\b"

    @staticmethod
    def _overlaps_consumed(
        span: tuple[int, int],
        consumed: list[tuple[int, int]],
    ) -> bool:
        a, b = span
        for c, d in consumed:
            if a < d and c < b:
                return True
        return False
