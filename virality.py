"""
Virality scoring for candidate clips.

Combines three sources of insight into one 0-99 score per clip:

* Higgsfield's Virality Predictor (perception-style outputs): a hook score,
  a predicted hold rate, and the timestamp of the peak moment.
* SupoClip / OpusClip (transcript rubric): an LLM scores hook, engagement,
  value and shareability, then ranks all candidates against each other
  because isolated scores are not comparable.
* What TikTok, YouTube Shorts and Instagram Reels reward: the first ~3 s
  (viewed vs. swiped away), completion and average % viewed, rewatches/loops,
  and shares/sends per reach.

Component         weight  platform signal it targets
  hook              30%   viewed vs swiped away, early hold
  hold              25%   completion rate / average % viewed
  emotion/payoff    20%   comments & likes; payoff keeps people to the end
  shareability      15%   sends per reach (Reels), shares (TikTok)
  loop              10%   rewatches

The signal-based metrics always run locally. The Claude rubric is optional:
it runs when Anthropic credentials are available (ANTHROPIC_API_KEY or an
`ant auth login` profile) and otherwise falls back to heuristics.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

import numpy as np

BIN = 0.5
# Hand-tuned starting point, NOT published by any platform. TikTok, YouTube and
# Instagram don't release ranking weights; they only say which signals matter
# (see the module docstring). The same weights are used for all three platforms
# for now. Revisit them if the platforms change what they reward.
WEIGHTS = {"hook": 0.30, "hold": 0.25, "emotion": 0.20, "share": 0.15, "loop": 0.10}
CLAUDE_MODEL = "claude-opus-5-5"

# Spoken reactions that tend to drive comments and shares on gaming/stream clips
REACTIONS = re.compile(
    r"\b(oh my (god|gosh)|no way|let'?s go+|what+|wtf|holy|bro+|yo+|dude|insane|crazy|"
    r"are you (kidding|serious)|i can'?t|bruh|nah+|oh no|why+|how+|clutch|ha(ha)+|lol|lmao|"
    r"shit|fuck\w*|damn|wow)\b",
    re.I,
)


def _pct(x: float, lo: float, hi: float) -> float:
    """Map x linearly from [lo, hi] to [0, 100], clamped."""
    return float(np.clip((x - lo) / (hi - lo) * 100.0, 0.0, 100.0))


@dataclass
class Virality:
    score: int = 0  # 0-99 overall
    grade: str = ""  # S / A / B / C / D
    hook: int = 0  # 0-100 per component
    hold: int = 0
    emotion: int = 0
    share: int = 0
    loop: int = 0
    hold_rate: int = 0  # predicted % of viewers still watching at the end
    peak_time: float = 0.0  # seconds into the clip of the strongest moment
    hook_type: str = ""  # from Claude: reaction / question / statement / ...
    title: str = ""  # suggested post title (Claude)
    reasoning: str = ""  # short explanation (Claude)
    tips: list = field(default_factory=list)
    used_llm: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Local, signal-based metrics (always available)
# --------------------------------------------------------------------------
def local_metrics(scores: dict, start: float, end: float, words: list) -> Virality:
    """scores: per-bin arrays from clipper.score(); words: transcript relative to `start`."""
    a, b = int(start / BIN), max(int(start / BIN) + 1, int(end / BIN))
    tot, aud, burst, mot = (scores[k][a:b] for k in ("total", "audio", "burst", "motion"))
    n = len(tot)
    length = end - start
    first = slice(0, min(n, int(3 / BIN)))  # first 3 s
    last = slice(max(0, n - int(1.5 / BIN)), n)  # last 1.5 s

    # speech coverage per bin
    speech = np.zeros(n, bool)
    for w in words:
        speech[int(w.start / BIN) : int(np.ceil(w.end / BIN))] = True
    first_word = words[0].start if words else None
    text = " ".join(w.text for w in words)

    # HOOK: is something happening immediately? energy + motion + speech in the first 3 s
    if first_word is None:
        speak = 35.0
    else:
        speak = (
            100.0 if first_word <= 1.0 else 70.0 if first_word <= 2.0 else 40.0 if first_word <= 3.0 else 15.0
        )
    hook = (
        0.40 * _pct(float(tot[first].max()), 0.0, 3.0)
        + 0.25 * _pct(float(np.mean(aud[first])), -0.5, 2.0)
        + 0.10 * _pct(float(np.mean(mot[first])), -0.5, 2.0)
        + 0.25 * speak
    )

    # HOLD: how much of the clip stays "alive" (speech or above-baseline energy),
    # penalising the longest lull; shorter clips complete more often
    alive = speech | (tot > 0.3)
    lull, run = 0, 0
    for v in alive:
        run = 0 if v else run + 1
        lull = max(lull, run)
    lull_s = lull * BIN
    len_factor = float(np.interp(length, [8, 10, 15, 30, 60], [1.0, 1.0, 0.93, 0.8, 0.65]))
    hold = (0.55 * 100 * float(alive.mean()) + 0.45 * _pct(-lull_s, -4.0, 0.0)) * len_factor
    hold_rate = int(round(np.interp(hold, [0, 50, 80, 100], [15, 45, 70, 85])))  # noqa: RUF046  (int() needed on NumPy 1.x)

    # EMOTION / PAYOFF: peak intensity, spoken reactions, and a peak after the build-up
    peak_i = int(np.argmax(tot))
    peak_pos = peak_i / max(1, n - 1)
    reactions = len(REACTIONS.findall(text)) + text.count("!")
    emotion = (
        0.50 * _pct(float(tot.max()), 0.5, 4.5)
        + 0.30 * _pct(reactions, 0, 4)
        + 0.20 * (100.0 if 0.35 <= peak_pos <= 0.9 else 50.0)
    )

    # SHAREABILITY (heuristic; replaced by Claude when available)
    share = 0.5 * emotion + 0.3 * _pct(float(burst.max()), 0.0, 3.0) + 0.2 * _pct(len(words), 3, 25)

    # LOOP: ending on a high (reaction / action) makes people replay
    loop = 0.6 * _pct(float(np.mean(tot[last])), -0.5, 2.0) + 0.4 * (100.0 if speech[last].any() else 40.0)

    v = Virality(
        hook=int(hook),
        hold=int(hold),
        emotion=int(emotion),
        share=int(share),
        loop=int(loop),
        hold_rate=hold_rate,
        peak_time=round(peak_i * BIN, 1),
    )

    # actionable tips, Higgsfield-style
    if first_word is not None and first_word > 2.0:
        v.tips.append(f"First words start at {first_word:.1f}s - trim the start so speech opens the clip.")
    if first_word is None:
        v.tips.append("No speech detected - add on-screen text so viewers know what's happening.")
    if lull_s >= 2.5:
        v.tips.append(f"{lull_s:.1f}s quiet stretch - cut it or add a zoom/text pop to hold attention.")
    if peak_pos < 0.25:
        v.tips.append("The peak comes very early - consider opening on it and ending on the reaction.")
    if loop < 45:
        v.tips.append("Ending is flat - end on the reaction so the loop back to the start feels natural.")
    if length > 20:
        v.tips.append("Shorter clips complete more often - try 10-15 s.")
    return v


# --------------------------------------------------------------------------
# Claude rubric + comparative ranking (optional; one API call per video)
# --------------------------------------------------------------------------
_SYSTEM = """You are a senior short-form video editor scoring clip candidates cut from one gaming or livestream video for TikTok, YouTube Shorts and Instagram Reels.

Each candidate gives you its transcript (what the streamer says) and measured signals from the audio and video, because you cannot see the footage. Signals are 0-100 unless noted: hook (energy and speech in the first 3 seconds), hold (how much of the clip stays active), emotion (peak intensity and spoken reactions), loop (energy at the end), plus the clip length and the second the peak happens.

What the platforms reward: viewers not swiping away in the first 1-3 seconds, watching to the end and rewatching, and sending the clip to friends. For stream clips that usually means a genuine reaction (shock, rage, laughter, hype), a clear moment a stranger understands without context, and a payoff or punchline.

Score every candidate:
- hook_score 0-25: would the first seconds stop a scroll? Strong reactions or clear stakes score high; mid-sentence or contextless openings score low.
- engagement_score 0-25: emotion, energy, humor or drama.
- value_score 0-25: is it understandable and satisfying on its own (setup and payoff), or does it need unseen context?
- shareability_score 0-25: would someone send this to a friend?
- hook_type: one of reaction, question, statement, story, contrast, none.
- comparative_score 0-100: rank against the other candidates in this batch. Spread scores out: the best usually lands 80-95, weak or contextless ones below 50.
- title: a 3-8 word post caption grounded in what is actually said, sentence case, no hashtags or emojis.
- reasoning: one short sentence citing what is in the clip.

Judge clip quality only. Do not downgrade a candidate for profanity or mature game content. Score every candidate exactly once, using its id."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "hook_score": {"type": "integer"},
                    "engagement_score": {"type": "integer"},
                    "value_score": {"type": "integer"},
                    "shareability_score": {"type": "integer"},
                    "hook_type": {
                        "type": "string",
                        "enum": ["reaction", "question", "statement", "story", "contrast", "none"],
                    },
                    "comparative_score": {"type": "integer"},
                    "title": {"type": "string"},
                    "reasoning": {"type": "string"},
                },
                "required": [
                    "id",
                    "hook_score",
                    "engagement_score",
                    "value_score",
                    "shareability_score",
                    "hook_type",
                    "comparative_score",
                    "title",
                    "reasoning",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


def llm_available() -> bool:
    try:
        import anthropic

        anthropic.Anthropic()  # resolves env key / `ant auth login` profile
        return True
    except Exception:
        return False


def llm_rank(title: str, items: list[dict], note: str = "") -> dict[int, dict] | None:
    """items: [{id, transcript, length, peak_time, hook, hold, emotion, loop}] -> {id: rubric}.
    Returns None when Claude is unavailable or the call fails (callers fall back)."""
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic()
    except Exception:
        return None

    lines = [f"Video title: {title}", ""] + ([note, ""] if note else []) + ["Candidates:"]
    for it in items:
        lines.append(json.dumps(it, ensure_ascii=False))
    try:
        resp = client.beta.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=_SYSTEM,
            messages=[{"role": "user", "content": "\n".join(lines)}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
    except anthropic.AuthenticationError:
        print("[virality] Claude credentials rejected - using local scoring only")
        return None
    except anthropic.RateLimitError:
        print("[virality] Claude rate-limited - using local scoring only")
        return None
    except anthropic.APIStatusError as e:
        print(f"[virality] Claude API error {e.status_code} - using local scoring only")
        return None
    except anthropic.APIConnectionError:
        print("[virality] Can't reach the Claude API - using local scoring only")
        return None
    except (anthropic.AnthropicError, TypeError) as e:
        # the SDK raises TypeError when no API key / auth profile is configured
        print(f"[virality] Claude not configured ({type(e).__name__}) - using local scoring only")
        return None
    if resp.stop_reason != "end_turn":
        print(f"[virality] Claude stopped early ({resp.stop_reason}) - using local scoring only")
        return None
    text = next((blk.text for blk in resp.content if blk.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return {int(c["id"]): c for c in data.get("candidates", [])}


# --------------------------------------------------------------------------
# Combine
# --------------------------------------------------------------------------
def _grade(s: int) -> str:
    return "S" if s >= 85 else "A" if s >= 72 else "B" if s >= 58 else "C" if s >= 42 else "D"


def finalize(v: Virality, llm: dict | None = None) -> Virality:
    """Blend local metrics with Claude's rubric (when present) into the final score."""
    if llm:
        v.used_llm = True

        def clamp(x):
            return int(np.clip(x, 0, 25)) * 4  # 0-25 -> 0-100

        v.hook = int(0.5 * v.hook + 0.5 * clamp(llm["hook_score"]))
        v.emotion = int(
            0.5 * v.emotion + 0.25 * clamp(llm["engagement_score"]) + 0.25 * clamp(llm["value_score"])
        )
        v.share = clamp(llm["shareability_score"])
        v.hook_type = llm.get("hook_type", "")
        v.title = llm.get("title", "")
        v.reasoning = llm.get("reasoning", "")
    combined = sum(WEIGHTS[k] * getattr(v, k) for k in WEIGHTS)
    if llm:
        # SupoClip's insight: isolated scores aren't comparable, so blend in the
        # comparative ranking across all candidates from this video
        combined = 0.6 * combined + 0.4 * float(np.clip(llm["comparative_score"], 0, 100))
    v.score = int(np.clip(round(combined), 0, 99))
    v.grade = _grade(v.score)
    return v
