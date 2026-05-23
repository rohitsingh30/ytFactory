"""Stage 5.5 — author per-beat image prompts.

Inserted between beat split and image gen. Without this, the
heuristic fallback in ``pipeline/images.beat_to_prompt`` stuffs the
beat's literal narration text into the diffusion prompt, which
collapses every beat to a near-identical "single character ..." shot.

Calls the `claude` CLI to produce one ``{key_visual, scene}`` object
per beat. The schema matches what ``pipeline.images.load_prompts``
already validates, so the orchestrator picks them up transparently.

Constraints baked into the LLM prompt to keep generated prompts
linter-clean (DESIGN.md §14):

* #3 — no text-bait words (label, sign, text, writing, logo, ...)
* #11 — scene under ~50 tokens
* #12 — one main subject per beat (no "three friends", "several people")
* #13 — `key_visual` is the punchline; ``scene`` is supporting detail
* #7 — beat 0 must contain ≥2 concrete tokens (room/prop/posture)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pipeline import observability as _obs

from .. import images
from . import cli as llm
from ..beats import Beat


# Grammatical-subject heuristic for the PRIMARY SUBJECT IN FRAME
# annotation (Principle #14, audio-visual coherence). The subject
# determines who the visual protagonist must be — without this binding
# the LLM author drifts and renders the wrong agent (e.g. dad embracing
# grandma when the narration says "my three kids are burnt out
# caregiving"). Captures pronouns and possessive-NPs at the head of
# the beat; leaves the annotation off when no clean subject is present.
_FILLER_HEAD = r"(?:so|and|but|then|now|well|like|see|listen|i\s+mean)\s+"
_NUMBER_WORD = (
    r"two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|\d+"
)
_SUBJECT_RE = re.compile(
    rf"^\s*(?:{_FILLER_HEAD})?"
    r"(?P<subj>"
    # 1st/3rd person pronouns
    r"i|we|she|he|they|"
    # "my three kids" / "her two sisters" / "their five cats"
    rf"(?:my|our|her|his|their)\s+(?:{_NUMBER_WORD})\s+\w+s?|"
    # "my mom" / "my older brother" / "her annoying ex"
    r"(?:my|our|her|his|their)(?:\s+\w+){1,3}|"
    # "the girl" / "the angry neighbor"
    r"the(?:\s+\w+){1,2}"
    r")\b",
    flags=re.IGNORECASE,
)


# Tokens to trim from the tail of a greedy subject match. The `\w+`
# extension in _SUBJECT_RE can over-extend past the head noun into the
# verb ("the wedding is") or an adverb that precedes the verb ("my mom
# always"); we trim trailing verb/adverb tokens to land back on the
# noun. Conservative list — false negatives just drop the annotation,
# which is fine; false positives would lie about the subject.
_TRIM_TOKENS = frozenset({
    # auxiliaries / copulas
    "is", "are", "was", "were", "be", "been", "being",
    "has", "have", "had",
    "will", "would", "can", "could", "should", "may", "might", "must",
    "do", "does", "did",
    # common past-tense verbs that follow subject NPs
    "got", "gets", "said", "told", "asked", "thought", "felt",
    "started", "stopped", "began", "ended", "kept", "made", "took",
    "gave", "went", "came", "saw", "ate", "knew", "found", "left",
    "called", "yelled", "screamed", "shouted", "stormed", "showed",
    "broke", "ran", "stayed", "sat", "stood", "bought", "sold",
    "paid", "charged", "billed", "refused", "forced", "moved",
    "wanted", "needed", "tried", "decided", "agreed",
    "argued", "explained", "answered", "responded",
    # adverbs that wedge between subject and verb
    "always", "never", "sometimes", "often", "rarely", "occasionally",
    "just", "really", "very", "totally", "definitely", "probably",
    "maybe", "also", "only", "even", "still", "already", "finally",
    "suddenly", "quickly", "slowly", "almost", "nearly", "barely",
    "literally", "actually", "honestly", "obviously",
})


def _extract_subject(beat_text: str) -> str | None:
    """Return the grammatical subject phrase of the beat, or None.

    Used to annotate each beat with PRIMARY SUBJECT IN FRAME so the
    image-prompt LLM can't pick a different visual agent. Conservative
    by design — when no clean head-NP matches, returns None and the
    annotation is omitted (rule still applies via the system prompt).
    """
    m = _SUBJECT_RE.match(beat_text or "")
    if not m:
        return None
    subj = m.group("subj").strip().rstrip(",.;:!?")
    # Truncate at coordinating conjunctions: "my husband and I" → "my husband".
    # The visual rule wants ONE primary subject; the LLM can still reference
    # the partner via the antagonist-presence rule.
    lower = subj.lower()
    for conj in (" and ", " or ", " & "):
        i = lower.find(conj)
        if i > 0:
            subj = subj[:i].rstrip(",.;:!?")
            break
    tokens = subj.split()
    while tokens and tokens[-1].lower() in _TRIM_TOKENS:
        tokens.pop()
    return " ".join(tokens) if tokens else None


# Few-shot example: verb-led, character-in-scene shots that demonstrate
# the correct prompt grammar for Z-Image-Turbo / FLUX.2 — composition
# verb → subject body → environment with TWO spatial anchors → lighting.
#
# Pre-2026-05-16 these examples were noun-led ("a tiny green salad bowl
# with a single fork", "alone on a wooden table") — they taught the LLM
# to produce floating-object product photography. Job 3cd2b3b5 (AITA
# ketchup-on-stew) rendered a hovering Henley T-shirt + a lone ketchup
# bottle on a counter + an empty stew pot because the few-shot was the
# bug. Replaced with verb-led shot descriptions that put a human body
# in frame for every beat.
_FEWSHOT = [
    {
        "narration_beat": "I refused to split the bill",
        "key_visual": "medium shot of the character leaning back from a restaurant table, palms up in refusal",
        "scene": "warm pendant light overhead, blurred patrons in the background, a leather banquette, shallow depth",
    },
    {
        "narration_beat": "I had a fourteen dollar salad",
        "key_visual": "close-up of the character's hand lifting a small green salad on a fork",
        "scene": "the character soft-focus behind the fork, a single white plate on a dark wood table, warm overhead restaurant light",
    },
    {
        "narration_beat": "they had four hundred dollars of steak and wine",
        "key_visual": "over-shoulder shot of the character watching two strangers' hands carve into a steaming steak",
        "scene": "their faces cropped out of frame, an empty wine glass next to the plate, dim restaurant ambience, candlelight on the table",
    },
    {
        "narration_beat": "and three bottles of wine",
        "key_visual": "low-angle medium shot of two stranger hands clinking wine glasses across the table",
        "scene": "the character's elbow visible at frame edge in stunned reaction, three dark wine bottles soft-focus on the table behind, candlelit dining room",
    },
]


_SYSTEM = """\
You author per-beat image prompts for a YouTube Shorts factory that
renders illustrated AITA-style stories. Each beat is a ~2-second slice
of narration that needs ONE image. Your job: produce a JSON array of
{key_visual, scene} objects, one per beat, in beat order.

Schema (per beat):
- "key_visual": a short, concrete noun phrase naming the most arresting
  visual in this beat — the punchline. 4-10 words. Example:
  "a single crumpled twenty-dollar bill on a wooden table".
- "scene": a short supporting clause describing where/how the
  punchline sits in frame and what the character (if present) is
  doing. 5-15 words. Example: "the character standing up beside the
  table holding their coat".

Hard rules (violations make the rendered Short worse):
1. ZERO TEXT IN ANY FRAME — hard constraint, no exceptions.

   Diffusion models (Z-Image-Turbo, FLUX.2, SDXL) render any
   text-bait token as gibberish — fake Hindi-Latin scribble on
   bottles, melted lettering on signs, garbled receipts. The viewer
   sees jumbled fake-text and the immersion breaks. The ONLY text
   the viewer should ever see is the caption-pill overlay added by
   post-processing AFTER the diffusion model finishes — that is
   compose.py's job, not yours.

   NEVER use these tokens in key_visual or scene: label, sign,
   signs, signage, text, writing, written, letters, lettering, logo,
   brand, dollar sign, words, title, title card, subtitle, caption,
   menu, newspaper, headline, billboard, poster, speech bubble,
   phone screen with text, text message, notification, store name,
   license plate, name tag, price tag, address, number plate,
   receipt, ticket, invitation, certificate, diploma, contract,
   prescription, greeting card, business card, boarding pass, book
   cover, magazine cover, t-shirt slogan, tattoo of words.

   Phrase constraints as POSITIVE PRESENCE (Z-Image-Turbo has no
   negative-prompt support — "no text" is silently ignored;
   "unmarked surface" is rendered):
     GOOD: "plain unmarked ceramic mug", "smooth glass bottle without
            markings", "blank wood surface", "unbranded cardboard
            box", "plain cotton t-shirt in mustard yellow",
            "unlabeled jar of preserves".
     BAD:  "ketchup bottle" (model paints "Heinz" or fake-brand
            scribble), "phone screen" (model paints fake apps with
            gibberish names), "cereal box" (fake-brand wall of text).

   Never use "X reading Y", "X that says Y", "X displaying Y", "X
   labeled Y", "X with Y written on it" — those clauses guarantee
   gibberish. Describe text-bearing props by SHAPE/COLOR/CONTEXT
   only — "a folded cream paper with a gold border on the table"
   not "a wedding invitation".

   The pipeline runs a post-render OCR lint that re-renders any
   image where ≥3 characters are detected, so this rule has both a
   prompt-side and a render-side enforcement. Treat it as
   non-negotiable.

2. ONE main subject per beat. Never "three friends", "several
   people", "a group of customers", "five kids". If the narration
   names multiple people, focus on ONE (the narrator alone, or just
   the object they're talking about).

3. The "scene" field MUST be **60–120 words**, structured as four
   slots in order:

     (a) ENVIRONMENT plane — specific named surface + specific named
         background plane (Rule 14). Two spatial anchors minimum.
         E.g. "at a butcher-block counter, cast-iron Dutch oven
         steaming on the stove behind her, dark wood cabinets in
         soft focus".
     (b) LIGHTING — direction + colour-temp or time-of-day (Rule 15).
         E.g. "single warm pendant light from above, soft shadow
         falloff on the counter".
     (c) CHARACTER ACTION + EMOTION — posture/face verbs that match
         the spoken beat (Rule 8). E.g. "brow furrowed, mouth
         pressed flat in disbelief, one hand frozen mid-gesture".
     (d) SUPPORTING DETAIL — one or two grounding props or texture
         tokens that lock the scene's specificity. E.g. "a wooden
         spoon resting on the rim of the pot, faint steam curl,
         soft shallow depth".

   Z-Image-Turbo + FLUX.2 absorb 80–250 words of structured detail
   per Tongyi-MAI / Black Forest Labs prompting guides. Under-prompting
   (the pre-2026-05-17 ~40-word cap) yielded flat clip-art renders.
   Don't pad — every word should pull weight in one of the four slots.

4. If the narration beat names a SPECIFIC object (a salad, a pool,
   a wine bottle, a phone), make THAT object the key_visual. Don't
   default to a portrait of the character every beat.

5. Beat 0 (the hook) must NOT be a generic "character standing
   smiling" shot. It must contain at least 2 concrete tokens — a
   specific room, a specific prop, a specific posture. The Short
   lives or dies on the first 1.5 seconds.

6. The character (when present) is described elsewhere — DO NOT
   re-describe their appearance. Just say "the character" or refer
   to their action ("hands up in refusal", "leaning forward",
   "looking at phone").

7. ANTAGONIST PRESENCE: when a beat names third parties ("my friends",
   "she told me", "they said"), the scene MUST include at least one
   visual cue of that party — even just a back-of-head, a silhouette,
   a hand entering frame, or an empty plate across the table. Without
   this cue, the narrator visually owns the action attributed to
   others (e.g. she looks like SHE ordered the $400 meal). Don't
   render the third party as a full character (Principle #12 still
   applies — one main subject) — just an indexical hint.

8. EMOTION TRACKS NARRATION: encode the character's emotion in the
   `scene` field via posture/face verbs that match what the character
   is SAYING right now. Annoyed when scolded, resigned when wronged,
   smug when winning. NEVER default to neutral smile — that's the
   single biggest viewer-immersion break. Examples: "arms crossed,
   eyes narrowed", "shrugging helplessly", "leaning back amused",
   "hand on hip, head tilted in disbelief".

9. PRIMARY SUBJECT BINDING: when a beat is annotated below with
   "[PRIMARY SUBJECT IN FRAME: <subject>]", the visual protagonist
   in your `scene` field MUST be that subject and no one else. If the
   subject is a count ("my three kids"), exactly that count of people
   must be visible AND they must be the foregrounded actors — not the
   narrator, not the parent, not a related character. Do not substitute
   "the family" for "my three kids", or "the dad" for "my kids". The
   narrator's body appears in frame ONLY when the annotated subject is
   "I" or "we". If the spoken beat assigns an action to a third party
   (e.g. "they said splitting was polite"), foreground at least one
   of those third parties as the visual agent. This is the highest-
   leverage rule on this page: violating it inverts the story for
   muted viewers.

10. COMPOSITION VARIETY: across any rolling 3-beat window, no two
    beats may share the same composition pattern. "Narrator alone with
    phone" / "narrator alone holding object X" / "narrator at a kitchen
    counter" cannot repeat back-to-back across consecutive beats — even
    if the spoken content is similar. Force a delta on every beat:
    change the angle (over-shoulder, profile, top-down, POV), the
    distance (wide → close-up → extreme close-up), the subject (object
    insert, third party reaction, hands only, scene-without-character),
    or the location (hallway → kitchen → couch). The 2026-05 critic
    flagged five consecutive "narrator + phone" frames as the single
    biggest mid-Short retention break. If a sequence of beats genuinely
    has the same character on the same prop, vary the *framing* —
    the model can render the same person doing the same thing from a
    different distance.

11. NEVER render metaphors literally. Sports / action narration is
    full of figurative language ("a screamer", "a rocket", "a thunderbolt",
    "a bullet header", "a missile from twenty yards", "the strike that
    kept the treble alive", "a torpedo into the top corner"). These
    describe a HARD STRUCK BALL, not an actual rocket / missile / bullet.
    If you see metaphor-of-action words in the narration, REWRITE the
    visual into the underlying observable action: foot connecting with
    ball, ball arcing toward goal, defender beaten, net rippling.
    BANNED key_visual / scene tokens: "rocket", "missile", "torpedo",
    "bullet", "projectile", "explosion", "fireball" — unless the
    narration is literally about a vehicle or weapon. Critic 2026-05-02:
    a beat for "the strike that kept the treble alive" rendered a
    cartoon rocket flying out of the goal; broke immersion entirely.

11b. NO SOCIAL-MEDIA / UI ICONOGRAPHY in scene fields. The closer
    panel is rendered programmatically by compose.py as a separate
    overlay; the underlying beat illustration MUST NOT include a heart
    icon, thumbs-up, like button, subscribe button, YouTube play
    button, comment bubble, share arrow, bell icon, or any other
    social-media UI element. Critic 2026-05-03 (top3-stoppage-goals v1):
    closer beat scene "burnt-orange heart icon lighting the fingertip"
    rendered a literal heart + thumbs-up + YouTube play button cluster
    that competed with the actual closer panel for attention. For closer
    beats, default to a neutral end-of-story scene: empty stadium at
    dusk, ball on the centre spot, trophy on a plinth, or a wide
    parchment-coloured backdrop. Let the closer panel carry the CTA.

11c. NEVER render a trophy / cup / medal / lifted-shield in any beat
    BEFORE the climax has been spoken. Critic 2026-05-03: a "2014
    Champions League final" announcement beat rendered the European
    Cup on a plinth — but the goal that wins it (Ramos's header)
    hasn't been described yet. Showing the trophy first inverts the
    story for muted viewers. Trophy/cup imagery is allowed ONLY in
    post-goal-celebration beats (after the rank's match_text in the
    narration order). For pre-climax announcement beats, use
    stadium-context shots: empty pitch, lit floodlights, stand of
    fans, captain in the centre circle.

11d. HOOK BEAT MUST CONTAIN A HUMAN. Beat 0 must show at least ONE
    human figure on a soccer pitch — striker, goalkeeper, fan, ref.
    Two consecutive abstract-object hooks ("netting bulging" + "clock
    face") in the first 6s gives the viewer no face to anchor to.
    Faces are the highest-attention element in any frame; the first
    1.5s decision-window dies without one. Critic 2026-05-03 (top3-
    stoppage-goals v1) caught beat-0/beat-1 as netting+clock with no
    human; viewers swiped before any character appeared.

11e. SOCCER VERB DISAMBIGUATION. In a soccer/football channel, action
    verbs in narration are ALWAYS soccer-context, never literal:
      - "whips in / whips a ball / whipped the corner" → soccer
        cross / corner kick (NEVER curling broom + stone)
      - "curls / curled the free kick" → bending soccer ball flight
        (NEVER hairdressing or curling-the-sport)
      - "bends / bent it like Beckham" → soccer ball arcing trajectory
      - "rifles / rifled / rifled in" → hard-struck soccer shot, never
        actual rifle
      - "thunderbolt / thunders home" → hard soccer strike, never
        weather/thunder
      - "smashes / smashed it home" → soccer goal, never demolition
      - "slots / slots home" → calmly placed soccer goal
    Critic 2026-05-03: "Modric whips in the corner" rendered as a
    curling sport scene (broom, stone, ice rink). The verb 'whips'
    in soccer corner-kick context = a player kicking a curving ball
    delivered into the penalty area, never anything else.

12. SPORTS IDENTITY GROUNDING: when the narration names a real player
    (Beckham, Aguero, Ramos, Iniesta, Solskjaer, etc.) AND the user
    has supplied an `image_prompt_hint` for that beat, treat that hint
    as authoritative ground truth — copy its kit description, era,
    pose, and setting verbatim into your `scene` field. The hint is
    the curator's specification; deviating from it produces wrong-team
    / wrong-era kits that contradict the spoken story (Critic 2026-05-02:
    Ramos rendered in Atletico kit, Aguero in orange not Man City blue).
    The hint always wins over your own paraphrase.

13. VERB-LED, NOT NOUN-LED. The `key_visual` MUST begin with a shot
    type and a verb-driven action on a human body, never a bare
    noun phrase. Z-Image-Turbo / FLUX.2 resolve a bare noun
    ("a ketchup bottle", "a worn Henley T-shirt", "an empty stew pot")
    to its default seamless-backdrop product photo — a hovering object
    on a plain background, no narrative. Lead with the shot type +
    verb + subject:
      GOOD: "medium over-shoulder shot of the character squeezing a
             red squeeze bottle into a steaming pot of beef stew"
      GOOD: "POV close-up of two hands tearing the lid off a takeout
             container"
      GOOD: "wide low-angle of the character storming out of a kitchen
             doorway, hands raised in disbelief"
      BAD:  "a red ketchup bottle"
      BAD:  "an empty stew pot"
      BAD:  "a Henley T-shirt"
    Critic 2026-05-16 (job 3cd2b3b5, AITA ketchup-on-stew): every
    `key_visual` was a bare noun → every rendered frame was a floating
    product shot with no character, no kitchen, no story. The fix is
    grammatical: every key_visual is a SHOT, not a STILL LIFE.

14. NAMED ENVIRONMENT PLANE. Every `scene` MUST name a specific
    surface AND a background plane — two spatial anchors, not one. The
    diffusion model composes depth from these two anchors; with only
    one (or none) it defaults to a flat seamless backdrop.
      GOOD: "at a butcher-block counter, cast-iron Dutch oven steaming
             on the stove behind her"  ← surface + background
      GOOD: "on a leather couch, lit window over her shoulder"
             ← surface + background
      BAD:  "in a kitchen"  ← no surface, no background
      BAD:  "alone on a counter"  ← surface but no background

15. LIGHTING IS MANDATORY. Every `scene` MUST contain at least one
    lighting token tied to the location — direction + colour-temp or
    time-of-day. Without lighting the model defaults to flat product-
    photography lighting which kills the cinematic feel.
      GOOD: "warm pendant light from above"
      GOOD: "blue laptop glow from her left"
      GOOD: "soft afternoon window light, gentle falloff"
      GOOD: "candlelight on the table, dim restaurant ambience"
      BAD:  no lighting clause at all
    Lighting is the single highest-impact dial on diffusion renders
    after subject placement.

16. POSITIVE-ONLY CONSTRAINTS. Z-Image-Turbo is CFG-distilled at
    guidance_scale=0 and has NO negative-prompt support at the
    model level. Phrase every constraint as PRESENCE, not absence.
    "no harsh shadows" is silently ignored; "soft falloff" is
    rendered. "no clutter" is ignored; "plain wood counter" is rendered.
    Rule 1's banned-tokens list is enforced by post-author lint
    (images.strip_text_bait) — you don't need to write "no text" or
    "no labels" in your prompts; just omit the banned nouns.

17. ONE STYLE FAMILY. The channel `style_prefix` (auto-prepended to
    every render) already pins the artistic style. DO NOT add competing
    style tokens in `key_visual` or `scene`: never write "photoreal",
    "cinematic photo", "realistic render", "3D render", "anime style",
    "hyperreal", "octane render" — mixed style signals collapse the
    render into mush. Stick to camera/lighting/composition vocabulary
    in your fields; let style_prefix carry the medium.

Return ONLY a JSON object with one field, ``"beats"``, whose value is
an array of EXACTLY the requested number of beat objects in beat order.
No prose, no markdown fences, no commentary before or after.
"""


# Strict-compliant JSON schema for ``author_beat_prompts`` output.
#
# OpenAI/Azure structured outputs (response_format=json_schema, strict=true)
# DO NOT support root-array types — the schema must be a wrapper object
# with the array nested inside. Without this wrapper the LLM either:
#   * collapses the array into a single dict ({"key_visual": ..., "scene": ...})
#     — Shape C in _validate_and_clean, unrecoverable;
#   * wraps in an arbitrary-key envelope ({"beats": [...]} / {"prompts": [...]}
#     / etc), Shape A — recoverable but stochastic per call;
#   * emits an ordered-map keyed envelope ({"beat_1": {...}, ...}), Shape B.
# Pinning the wrapper key to ``"beats"`` removes that variance entirely;
# the validator below still handles A/A'/B/B'/C as defense-in-depth in
# case the schema isn't honoured (legacy deployments, content filter
# soft-error wrappers).
#
# Strict-compliance contract per Azure docs:
#   * every object: additionalProperties=false
#   * every property listed in required
#   * no unsupported keywords (minItems/maxItems for arrays, pattern/
#     format for strings, minimum/maximum for numbers)
# Reference: learn.microsoft.com/azure/foundry/openai/how-to/structured-outputs
# (Supported types, "All fields must be required", "Always set
# additionalProperties: false in objects").
_BEAT_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["beats"],
    "properties": {
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key_visual", "scene", "narration_line"],
                "properties": {
                    "key_visual": {"type": "string"},
                    "scene": {"type": "string"},
                    "narration_line": {"type": "string"},
                },
            },
        },
    },
}


def _find_rank_for_beat(
    beat_text: str,
    ranks: list[dict] | None,
    claimed: set[int] | None = None,
) -> dict | None:
    """Locate the ranks[] entry whose match_text appears in this beat.

    Used to thread tier-list curator hints (image_prompt_hint, kit_lock)
    through to the LLM prompt author and the post-author kit injector.
    Beat splitter may run finer than the rank structure — match by
    case-insensitive substring of `match_text` against beat text;
    first-match wins.

    ``claimed`` (optional, mutated): set of rank ints already paired
    with an earlier beat in this walk. Each rank fires exactly once per
    narration — the announcement beat is always before any closer
    reference in countdown structure, so first-match wins. Critic
    2026-05-02: without this, the closer "like if you agree with
    number one" also matched rank #1 and the LLM authored the closer
    as a duplicate Aguero shot.
    """
    if not ranks:
        return None
    needle_text = (beat_text or "").lower()
    for r in ranks:
        rid = r.get("rank")
        if claimed is not None and rid in claimed:
            continue
        mt = (r.get("match_text") or "").lower().strip()
        if mt and mt in needle_text:
            if claimed is not None:
                claimed.add(rid)
            return r
    return None


def _build_user_prompt(
    narration: str,
    beats: list[Beat],
    source_story: str,
    cast_narrator_desc: str | None,
    cast_default_emotion: str | None,
    style_prefix: str,
    opening_directives: dict | None,
    narrator_visual_mode: str = "on_screen",
    supporting: list[dict] | None = None,
    ranks: list[dict] | None = None,
) -> str:
    beat_lines = []
    # Claim-tracking: each rank fires its hint on the FIRST beat that
    # matches its match_text, never again. Closer references like
    # "if you agree with number one" must NOT inherit rank #1's hint.
    claimed_ranks: set[int] = set()
    for i, b in enumerate(beats):
        text = b.text.strip()
        subj = _extract_subject(text)
        subj_clause = (
            f"  [PRIMARY SUBJECT IN FRAME: {subj}]" if subj else ""
        )
        # Tier-list hint pass-through (Critic 2026-05-02): when this
        # beat opens a rank, the curator's image_prompt_hint is the
        # ground truth for kit / era / pose. Glue it onto the beat
        # line so the LLM sees it inline with the spoken text.
        rank = _find_rank_for_beat(text, ranks, claimed=claimed_ranks)
        hint_clause = ""
        if rank and rank.get("image_prompt_hint"):
            hint = rank["image_prompt_hint"].strip()
            hint_clause = (
                f"\n    [CURATOR HINT — authoritative ground truth for "
                f"this rank, use verbatim: {hint!r}]"
            )
        beat_lines.append(
            f'  Beat {i} ({b.duration:.2f}s): "{text}"{subj_clause}{hint_clause}'
        )
    beats_block = "\n".join(beat_lines)

    opening_block = ""
    if opening_directives:
        ex = opening_directives.get("example_tokens") or []
        n = int(opening_directives.get("required_concrete_tokens", 2))
        opening_block = (
            f"\nBeat 0 must contain at least {n} concrete tokens. "
            f"Example tokens this channel uses: {', '.join(ex)}.\n"
            f"Pick room/prop/posture detail from the story.\n"
        )

    if narrator_visual_mode == "voice_only":
        # Sports-style: narrator is a third-person voice over a depicted
        # event. The on-screen visual must be the actual event, not a
        # fictional analyst persona. List the real people from the
        # dossier so the LLM can name them in scene strings.
        sup_lines = []
        for s in supporting or []:
            name = s.get("name", "")
            desc = (s.get("description") or "").split(".")[0][:120]
            aliases = ", ".join(s.get("aliases") or [])
            sup_lines.append(f'  - {name} (aliases: {aliases}) — {desc}')
        sup_block = "\n".join(sup_lines) if sup_lines else "  (none)"
        cast_block = (
            "\nNARRATOR MODE: voice-over only. The narrator is a voice, "
            "NOT a visual character. DO NOT place a narrator / analyst / "
            "'the character' on screen in any beat. NEVER use phrases like "
            "'the character', 'the narrator', 'a man watching', 'a person "
            "in profile' — those words are FORBIDDEN. Every beat must "
            "depict either:\n"
            "  (a) ONE specific named real person from the cast list "
            "below (use their name + kit + #), OR\n"
            "  (b) A pure scene/object/match-context shot (empty stadium, "
            "scoreboard, ball on the spot, trophy, fans, locker room).\n\n"
            "REAL PEOPLE in this story (use these names verbatim in your "
            "scene strings — they map to real cartoon descriptors with "
            "locked seeds for cross-beat consistency):\n"
            f"{sup_block}\n\n"
        )
    else:
        cast_block = (
            f"\nNarrator (already locked into every image — DO NOT re-describe "
            f"the character's appearance): {cast_narrator_desc.strip()}\n"
            if cast_narrator_desc and cast_narrator_desc.strip()
            else ""
        )
        # Universal supporting-cast threading (post-2026-05-17, job
        # 3cd2b3b5 critique row #4). Pre-fix, the supporting block was
        # only emitted in voice_only narrator mode (sports channels) —
        # AITA / mystoriesanimated had `supporting` populated but
        # the `on_screen` branch silently dropped it, causing the
        # partner to render as a narrator clone at beat ~7s. Now any
        # channel with a non-empty cast.supporting gets the supporting
        # block, with explicit "DIFFERENT PERSON FROM NARRATOR" framing.
        if supporting:
            sup_lines = []
            for s in supporting:
                name = (s.get("name") or "").strip()
                desc = (s.get("description") or "").strip()
                aliases = ", ".join(s.get("aliases") or [])
                if not name or not desc:
                    continue
                alias_clause = f" (aliases: {aliases})" if aliases else ""
                sup_lines.append(f"  - {name}{alias_clause} — {desc}")
            if sup_lines:
                cast_block += (
                    "\nSUPPORTING CHARACTERS — DIFFERENT PEOPLE FROM "
                    "THE NARRATOR. When a beat references one of these "
                    "characters (by name OR by relationship — partner, "
                    "husband, wife, sister, brother, mom, friend, "
                    "coworker, neighbor, ex), the visual MUST use the "
                    "supporting character's locked appearance below, "
                    "NEVER the narrator's. Twin-clone rendering "
                    "(supporting character drawn as a copy of the "
                    "narrator) is the #1 mute-mode story-inversion "
                    "bug — Critic 2026-05-17 caught this on job "
                    "3cd2b3b5 partner-reveal beat:\n"
                    + "\n".join(sup_lines)
                    + "\n"
                )
    if cast_default_emotion and cast_default_emotion.strip():
        cast_block += (
            f"Narrator's default emotional tone for this story: "
            f"\"{cast_default_emotion.strip()}\". Per-beat scenes MUST "
            f"vary the character's expression to match what they are "
            f"saying RIGHT NOW (annoyed when scolded, resigned when "
            f"powerless, smug when winning). NEVER default to a neutral "
            f"smile — that's the #1 reason these Shorts read as flat. "
            f"Encode the emotion in the `scene` field via posture/face "
            f"verbs (e.g. 'arms crossed', 'eyes narrowed', 'shrugging "
            f"helplessly', 'leaning back amused').\n"
        )

    fewshot_block = json.dumps(_FEWSHOT, indent=2)

    return f"""\
SOURCE STORY (raw — mine this for visual richness, not just the narration):
\"\"\"
{source_story.strip()}
\"\"\"

NARRATION (what the viewer hears, total ~{len(narration.split())} words):
\"\"\"
{narration.strip()}
\"\"\"
{cast_block}
BEATS (one image each, in order):
{beats_block}

CHANNEL STYLE (auto-prepended to every render — YOU don't need to
re-state the artistic style, BUT for abstract/closer beats with no
concrete setting, embed at least one palette/setting cue from this style
into your `scene` so the rendered frame doesn't drift to a default dark
backdrop. The 2026-05 critic caught two consecutive closer beats
rendering on a flat dark-grey background because the scene was purely
"helpless shrug" with no setting or palette token to anchor the model):
{style_prefix.strip()[:200]}{'...' if len(style_prefix) > 200 else ''}
{opening_block}
EXAMPLE OUTPUT FORMAT (4-beat sample from a different story — match this granularity):
{fewshot_block}

Return a JSON object with a single ``"beats"`` field whose value is an
array of EXACTLY {len(beats)} objects, one per beat, in order. Each
object in the array has these fields:

- "narration_line": the EXACT beat text (verbatim, copied from the
  BEATS list above) that this prompt is illustrating. The renderer
  uses this to bind the image to the right spoken moment, so it MUST
  match the beat you are illustrating.
- "key_visual": a SHOT description (Rule 13) — shot type + verb +
  human-body subject, never a bare noun.
- "scene": environment with TWO spatial anchors (Rule 14) + lighting
  (Rule 15), ≤40 words total.
"""


# Hair colours we recognise — limited intentionally to the set the
# image models actually pick up reliably. "Strawberry blonde" etc. are
# intentionally NOT here; we'd false-positive on the cast having
# "blonde" while the scene says "strawberry blonde". A simple set
# catches the regression we observed (pink vs brown) without churn.
_HAIR_COLOURS = (
    "pink", "brown", "blonde", "blond", "black", "red", "ginger",
    "gray", "grey", "white", "auburn", "silver", "blue", "green",
    "purple", "dusty-pink", "dusty pink", "rose", "platinum",
)
_HAIR_COLOUR_RE = re.compile(
    r"\b(" + "|".join(_HAIR_COLOURS) + r")(?:[- ]?(?:coloured|colored))?\s+"
    r"(?:wavy|straight|curly|long|short|messy|tied|low|high|loose)?\s*hair\b",
    flags=re.IGNORECASE,
)
# Also catch the "<colour>-haired" form ("a brown-haired girl").
_HAIRED_ADJ_RE = re.compile(
    r"\b(" + "|".join(_HAIR_COLOURS) + r")(?:[- ]?coloured|[- ]?colored)?-haired\b",
    flags=re.IGNORECASE,
)
# Names + relational nouns that strongly imply the scene is about
# someone OTHER than the narrator — when one of these is the scene's
# subject, cast contradiction doesn't apply (cast covers narrator only;
# pipeline/cast.py supporting characters cover the rest).
_OTHER_CHARACTER_HINT_RE = re.compile(
    r"\b("
    r"sister|brother|mom|mother|dad|father|husband|wife|"
    r"boyfriend|girlfriend|ex|"
    r"friend|coworker|neighbor|neighbour|cousin|aunt|uncle|"
    r"stranger|guest|attacker|victim"
    r")\b",
    flags=re.IGNORECASE,
)


def _extract_hair_colour(text: str) -> str | None:
    """Return the hair colour mentioned in ``text``, lowercase, or None."""
    if not text:
        return None
    m = _HAIR_COLOUR_RE.search(text) or _HAIRED_ADJ_RE.search(text)
    if not m:
        return None
    # Normalise variants so "blond" matches "blonde", "grey" matches "gray".
    raw = m.group(1).lower().replace(" ", "-")
    return {"blond": "blonde", "grey": "gray"}.get(raw, raw)


def _check_cast_contradictions(
    *,
    scene: str,
    key_visual: str,
    narration_line: str,
    cast_narrator_desc: str,
) -> list[str]:
    """Warn if the scene asserts narrator features (currently: hair
    colour) that contradict cast.json:narrator.description.

    Skipped when the scene clearly references a non-narrator character
    (sister/Amelia/etc.) — cast contradiction doesn't apply because the
    scene's subject isn't the narrator. The class-of-bug fix for
    "image of sister looks like narrator" is in pipeline/cast.py
    (supporting-character emission), not here.

    Returns a list of human-readable contradiction strings. Empty list
    means the scene is consistent (or unverifiable).
    """
    cast_colour = _extract_hair_colour(cast_narrator_desc)
    if not cast_colour:
        return []  # cast didn't pin a hair colour — nothing to compare against
    out: list[str] = []
    combined = f"{key_visual} {scene}".strip()
    scene_colour = _extract_hair_colour(combined)
    if not scene_colour or scene_colour == cast_colour:
        return out
    # Subject suppression only applies if the scene's LEAD clause (the
    # part before the first comma/period) names another character.
    # Without this constraint, an incidental "two friends" or "a stranger"
    # at the END of a scene would falsely exempt the whole beat. The
    # actual sister-beats start with "Amelia ..." or "her sister ...";
    # narrator beats may still mention friends/strangers later.
    lead = re.split(r"[,.]", scene, maxsplit=1)[0]
    if _OTHER_CHARACTER_HINT_RE.search(lead):
        return out
    # Proper name in the lead (capitalised non-narrator name).
    lead_words = lead.split()
    if lead_words and any(
        w[:1].isupper() and w.lower() not in {"the", "a", "an"}
        for w in lead_words[:3]
        if w.isalpha()
    ):
        # A capitalised lead word that isn't a stop word — likely a
        # character name like "Amelia". Treat as supporting-character
        # scope and skip the contradiction.
        return out
    out.append(
        f"scene asserts {scene_colour!r} hair, cast says "
        f"{cast_colour!r}: {scene[:80]!r}"
    )
    return out


# Verb-led / shot-type lead tokens that the validator accepts as the
# first 1-2 tokens of a key_visual. Anything else starting with an
# article ("a"/"an"/"the") + noun pattern is rejected as a bare noun
# phrase. Class-of-bug fix 2026-05-17 (job 3cd2b3b5).
_SHOT_TYPE_LEADS = frozenset({
    # camera-distance shots
    "wide", "medium", "close-up", "close", "extreme", "tight", "tighter",
    "macro", "long", "establishing", "master", "full", "half",
    # camera-angle shots
    "low-angle", "high-angle", "low", "high", "overhead", "top-down",
    "birds-eye", "worms-eye", "eye-level", "dutch", "tilted", "canted",
    "over-shoulder", "over-the-shoulder", "ots",
    # camera-position shots
    "pov", "first-person", "third-person", "profile", "side", "rear",
    "front", "back", "behind",
    # camera-motion lead-ins
    "tracking", "dolly", "push-in", "pull-back", "panning", "tilting",
    # gerund verbs (action lead)
    "showing", "depicting", "rendering",
})


def _looks_bare_noun(text: str) -> bool:
    """Return True when ``text`` reads as a bare-noun phrase that the
    diffusion model will render as a floating-objects product shot.

    Heuristic: text starts with an article (a/an/the) followed by ≤4
    tokens of adjectives + nouns with NO verb / no shot-type lead.
    Permits any phrasing that begins with a shot-type token from
    :data:`_SHOT_TYPE_LEADS` (medium shot of …, POV close-up of …,
    over-shoulder of …) or with a gerund verb (the heuristic relies
    on the LLM author writing in continuous "doing" tense).

    Returns False for empty/None inputs — the empty-string check is
    upstream of this and yields a clearer "empty key_visual" error.
    """
    if not text:
        return False
    tokens = re.findall(r"\w+(?:-\w+)?", text.lower())
    if not tokens:
        return False
    first = tokens[0]
    # Shot-type or gerund lead → safe.
    if first in _SHOT_TYPE_LEADS:
        return False
    if first.endswith("ing") and len(first) > 4:
        # Allow gerunds — "squeezing", "pouring", "watching".
        return False
    # Article-led + no verb in first 5 tokens → bare noun.
    if first not in {"a", "an", "the"}:
        # Doesn't start with an article — could be a name or descriptor.
        # Permit unless none of the first 5 tokens looks verb-like.
        verb_present = any(
            t.endswith("ing") or t in _SHOT_TYPE_LEADS or t in {"of", "showing"}
            for t in tokens[:5]
        )
        return not verb_present
    # Article-led: check for a verb / shot-type token in the next 5
    # positions before declaring this a bare-noun phrase.
    for t in tokens[1:6]:
        if t in _SHOT_TYPE_LEADS:
            return False
        if t.endswith("ing") and len(t) > 4:
            return False
    return True


# Common primary-subject head nouns that fingerprint a beat's
# composition. Lowercased; matched as whole tokens.
_PRIMARY_SUBJECT_HEADS = frozenset({
    "character", "narrator", "woman", "man", "girl", "boy", "person",
    "hand", "hands", "face", "phone", "bottle", "bowl", "plate", "pot",
    "table", "counter", "kitchen", "bedroom", "couch", "doorway",
    "stove", "sink", "window", "door", "spoon", "cup", "mug", "fork",
    "glass", "screen", "shirt", "tee", "jeans", "head", "shoulder",
    "mouth", "eyes",
})


def _shot_fingerprint(key_visual: str) -> tuple[str, str]:
    """Return a (shot_lead, primary_subject) tuple that fingerprints
    the composition for the rolling-window duplicate check.

    Class-of-bug fix 2026-05-17 (job 3cd2b3b5 critique row #5):
    the pre-fix render had two consecutive "character pouring red
    into bowl" beats followed by 8 closer beats all rendering the
    same shrug pose. The LLM's _SYSTEM Rule 10 (COMPOSITION VARIETY)
    was guidance only; this fingerprint catches what the rule misses.

    Returns ("?", "?") when the heuristics can't extract either field;
    the caller's duplicate check skips these so a benign mismatch
    doesn't false-positive.
    """
    if not key_visual:
        return ("?", "?")
    tokens = re.findall(r"\w+(?:-\w+)?", key_visual.lower())
    if not tokens:
        return ("?", "?")
    shot_lead = tokens[0] if tokens[0] in _SHOT_TYPE_LEADS else "?"
    subject = next(
        (t for t in tokens if t in _PRIMARY_SUBJECT_HEADS),
        "?",
    )
    return (shot_lead, subject)


def _validate_and_clean(
    raw,
    beats: list[Beat],
    opening_directives: dict | None,
    cast_narrator_desc: str | None = None,
) -> list[dict]:
    # Post-2026-05-17: the verb-led + composition-fingerprint validators
    # below are gated by ``YTFACTORY_DISABLE_RICHNESS_GATE`` so unit
    # tests can use synthetic key_visuals (kv0/kv1) without tripping
    # them. Production never sets this flag.
    _validators_disabled = _env_flag_enabled("YTFACTORY_DISABLE_RICHNESS_GATE")

    # POST-2026-05-16: ``author_beat_prompts`` now sends a strict
    # ``{"beats": [...]}`` wrapper-object json_schema via
    # ``strict_schema=True``. On gpt-5.x / o1 / o3 deployments Azure's CFG
    # engine enforces the schema at token level so Shape A is GUARANTEED
    # for compliant deployments. The unwrap code below is retained as
    # defense-in-depth for: (a) legacy deployments that don't honour
    # strict-mode, (b) content-filter soft-error responses (200 OK with
    # ``{"error": "..."}`` body), and (c) the older Azure-backend
    # dispatcher quirk (2026-05-15) where ``output_json=True`` without
    # ``json_schema`` forced ``response_format={"type": "json_object"}``
    # and the model would emit one of the variant shapes:
    #
    #   A. Single-key array envelope (most common):
    #      ``{"beats":   [{...}, {...}]}``
    #      ``{"prompts": [{...}, {...}]}``
    #      ``{"items":   [{...}, {...}]}``
    #
    #   A'. Multi-key envelope where ONE field is the array and others
    #       are metadata (rationale, debug notes, etc.) — the array is
    #       always the largest list value:
    #      ``{"prompts": [{...}, {...}], "rationale": "..."}``
    #      ``{"items": [{...}, {...}], "metadata": {"version": 1}}``
    #
    #   B. Ordered-map (per-beat keyed object), surfaced by job f1e319a3
    #      canary on 2026-05-15:
    #      ``{"beat_1":  {"key_visual": "...", "scene": "..."},
    #         "beat_2":  {"key_visual": "...", "scene": "..."},  ...}``
    #      ``{"1":       {...}, "2":       {...},  ...}``
    #      ``{"shot_01": {...}, "shot_02": {...},  ...}``
    #
    #   B'. Ordered-map where the model's keys don't follow any ``key_visual``
    #       schema (Azure with non-English topic → Hindi or other locale
    #       slipping in field names like ``mukhya_drashya`` or fully
    #       free-form). Surfaced by job 79cdca90 (HindutavaAnimated
    #       Krishna leela Short) on 2026-05-15: every value was a dict
    #       but none had ``key_visual``/``scene``/``narration_line`` so
    #       the strict majority-beat-shaped gate failed and Shape B
    #       didn't unwrap. New gate: when ALL values are dicts AND there
    #       are no list values at all, treat as ordered-map regardless of
    #       per-value field names — let the per-item validation below
    #       surface clearer errors than "expected JSON array got dict".
    #
    #   C. Flat single-beat dict (model truncated or only emitted one):
    #      ``{"key_visual": "...", "scene": "..."}``  → cannot recover.
    #
    # Unwrap shapes A + A' + B + B' here rather than refactoring the
    # dispatcher, which affects every output_json=True call site. C
    # falls through to the count-mismatch error which is correct.
    if isinstance(raw, dict):
        # Shape A / A' — pick the LARGEST list value.
        list_pairs = [(k, v) for k, v in raw.items() if isinstance(v, list)]
        if list_pairs:
            k, v = max(list_pairs, key=lambda kv: len(kv[1]))
            print(
                f"[prompts] unwrapping Azure JSON-object array envelope "
                f"(picked key={k!r} from keys={list(raw)!r}) → "
                f"array of {len(v)} items"
            )
            raw = v
        else:
            # Shape B / B' — ordered map of per-beat dicts.
            values = list(raw.values())
            if values and all(isinstance(v, dict) for v in values):
                print(
                    f"[prompts] unwrapping Azure JSON-object ordered-map "
                    f"envelope (keys={list(raw)[:3]!r}…, n={len(values)}) "
                    f"→ array of {len(values)} beat dicts"
                )
                raw = values
            else:
                # Shape C or unknown — log the keys so the next dispatch
                # quirk is observable. The ValueError below carries the
                # type name; this print carries the actual structure.
                _kt = sorted({type(v).__name__ for v in raw.values()})
                print(
                    f"[prompts] could not unwrap Azure JSON-object envelope: "
                    f"keys={list(raw)[:6]!r}{'…' if len(raw) > 6 else ''} "
                    f"value-types={_kt!r} — falling through to ValueError"
                )

                # ``{"error": "<string>"}`` is Azure's soft-error shape —
                # the model returned an error description IN the JSON
                # object rather than throwing an HTTP error. Common when:
                #   - Content filter rejected the prompt (Azure returns
                #     200 with the rejection reason in 'error').
                #   - The model's output truncated mid-JSON and
                #     gpt-5.3-chat papered over by emitting just an
                #     error wrapper.
                #   - JSON schema validation failed model-side.
                #
                # Pre-2026-05-15 we swallowed this as "expected JSON
                # array, got dict" — uninformative noise. Now surface
                # the actual error text so operators can debug the
                # filter trigger or rephrase the prompt. The render
                # still falls through to the bare-Segment.text path
                # (per cloud worker's try/except wrapping) so it
                # doesn't crash the job.
                if list(raw.keys()) == ["error"] or (
                    "error" in raw and len(raw) <= 2
                ):
                    err_text = str(raw.get("error") or "")[:500]
                    print(
                        f"[prompts] Azure soft-error response: error={err_text!r} "
                        f"(this means LLM authoring is dead for this run — "
                        f"engine will fall back to bare Segment.text → "
                        f"generic-looking beat images. Most common cause: "
                        f"content filter on channel-prompt language or "
                        f"JSON schema rejection. Re-check the _SYSTEM "
                        f"template for filter-triggering language.)"
                    )
    if not isinstance(raw, list):
        raise ValueError(f"expected JSON array, got {type(raw).__name__}")
    if len(raw) != len(beats):
        raise ValueError(
            f"LLM returned {len(raw)} prompts but beats has {len(beats)}; "
            f"counts must match"
        )

    cleaned: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"item {i} is not an object: {item!r}")
        kv = (item.get("key_visual") or "").strip()
        sc = (item.get("scene") or "").strip()
        if not sc:
            raise ValueError(f"item {i} has empty 'scene'")
        # Preserve ``narration_line`` if the LLM provided one. Even if it
        # didn't, fall back to the beat's own text so the renderer's
        # anchor-based alignment (images._align_prompts_to_beats) can run
        # — without this the matcher never fires and the renderer ships
        # whatever order the LLM happened to emit.
        nl = (item.get("narration_line") or "").strip() or beats[i].text.strip()

        # Hard verb-led validator (post-2026-05-17, job 3cd2b3b5 critique
        # row #3 / Rule 13). Bare-noun key_visuals ("a red ketchup
        # bottle", "an empty stew pot", "a yellow shirt") render as
        # floating-objects product photos on Z-Image-Turbo. The
        # _SYSTEM rule asked the LLM nicely; this validator enforces it
        # at the structure level. Reject any key_visual whose first
        # six tokens look like a bare noun phrase with no shot-type or
        # verb-action lead — the worker's retry loop will re-call the
        # LLM with the validation message in context.
        #
        # Gated by YTFACTORY_DISABLE_RICHNESS_GATE so unit tests that
        # exercise envelope-unwrap shape-handling can use short
        # synthetic key_visuals (kv0/kv1) without tripping the verb-
        # led check. Production renders leave the gate on.
        if not _validators_disabled and _looks_bare_noun(kv):
            raise ValueError(
                f"item {i} key_visual is a bare noun phrase "
                f"(violates Rule 13: VERB-LED). Got: {kv!r}. "
                f"Rewrite as 'medium shot of X verb-ing Y' or "
                f"'over-shoulder of A doing B' — never just a noun."
            )

        cleaned.append({"key_visual": kv, "scene": sc, "narration_line": nl})

    # Hard composition-variety validator (post-2026-05-17, job 3cd2b3b5
    # critique row #5 / Rule 10). The pre-fix render had 9–12s show two
    # consecutive "character pouring red into bowl" beats and 27–47s
    # show the same shrug pose for 20 straight seconds. A 3-beat
    # rolling window should never have two identical shot-fingerprints.
    # Fingerprint = (shot-type lead-word, primary-subject head-noun).
    # Gated by YTFACTORY_DISABLE_RICHNESS_GATE — see verb-led gate above.
    if not _validators_disabled and len(cleaned) >= 2:
        prints = [_shot_fingerprint(p["key_visual"]) for p in cleaned]
        for i in range(1, len(prints)):
            if prints[i] == prints[i - 1] and prints[i] != ("?", "?"):
                raise ValueError(
                    f"item {i} key_visual repeats composition of item "
                    f"{i-1} (fingerprint={prints[i]!r}). Violates Rule "
                    f"10 (COMPOSITION VARIETY). Vary shot type, "
                    f"angle, distance, or primary subject across "
                    f"consecutive beats."
                )

    # Strip text-bait + leaked meta-instructions BEFORE persisting.
    # Class-of-bug fix 2026-05-02: lint warnings alone let SDXL render
    # quoted text and "Replace hook visual with…" imperatives as
    # gibberish in-image text. images.strip_text_bait removes these so
    # the cached prompts.json never contains them. The (still purely
    # informational) lint runs after the strip so we can see what's
    # left.
    for i, p in enumerate(cleaned):
        cleaned_scene, removed = images.strip_text_bait(p["scene"])
        if removed:
            print(
                f"[prompts] beat {i} stripped text-bait/meta from scene: "
                f"{removed}"
            )
            p["scene"] = cleaned_scene
        cleaned_kv, removed_kv = images.strip_text_bait(p["key_visual"])
        if removed_kv:
            print(
                f"[prompts] beat {i} stripped text-bait from key_visual: "
                f"{removed_kv}"
            )
            p["key_visual"] = cleaned_kv
        warnings = images.lint_prompt(p["scene"])
        for w in warnings:
            print(f"[prompts] beat {i} lint: {w}")

    # Cast contradiction lint (critic 2026-05): hook beat said "girl
    # with brown hair" while cast.json:narrator.description said
    # dusty-pink. The hook character was rendered with the WRONG hair
    # because the scene description directly contradicted cast and
    # nothing caught it. Fix: any scene that mentions an explicit hair
    # colour for the narrator must match cast. Scenes that explicitly
    # name a different character (Amelia, sister, mom, etc.) are
    # exempt — see pipeline/cast.py for the supporting-character path.
    if cast_narrator_desc:
        for i, p in enumerate(cleaned):
            for w in _check_cast_contradictions(
                scene=p["scene"],
                key_visual=p["key_visual"],
                narration_line=p["narration_line"],
                cast_narrator_desc=cast_narrator_desc,
            ):
                print(f"[prompts] beat {i} CAST CONTRADICTION: {w}")

    # Beat 0 opening-directive sanity check.
    if opening_directives and cleaned:
        examples = [t.lower() for t in opening_directives.get("example_tokens") or []]
        n_required = int(opening_directives.get("required_concrete_tokens", 2))
        opening_text = (cleaned[0]["key_visual"] + " " + cleaned[0]["scene"]).lower()
        # Fuzzy match: any substantive word from each example token.
        hits = []
        for ex in examples:
            tokens = [t for t in ex.split() if len(t) > 2]
            if any(t in opening_text for t in tokens):
                hits.append(ex)
        if len(hits) < n_required:
            print(
                f"[prompts] WARNING: beat 0 has only {len(hits)}/{n_required} "
                f"concrete tokens from channel examples (matched: {hits}). "
                f"Continuing anyway."
            )

    return cleaned


# Channel richness floor (post-2026-05-17, job 3cd2b3b5 root-cause).
# Under-prompting causes Z-Image-Turbo + FLUX.2 to collapse to their
# seamless-backdrop product-photo default. 60-word floor on
# image_style_prefix is the minimum that lets the diffusion model
# resolve a coherent style; 50-word floor on character_description is
# the minimum that resolves real facial features instead of "round-
# headed dot-eyed cartoon shape" (the literal pre-fix YAML text).
#
# Override with ``YTFACTORY_DISABLE_RICHNESS_GATE=1`` for one-off
# debug renders or legacy regression-test fixtures that pass a thin
# style_prefix on purpose. Production renders should leave this on.
_STYLE_PREFIX_MIN_WORDS = 60
_CHARACTER_DESCRIPTION_MIN_WORDS = 50

# Required token-class signals in style_prefix. Each is a tuple of
# (label, list-of-acceptable-substrings); style_prefix must contain
# at least one substring from each list. Catches "rich-but-vague"
# styles (200 words that never mention lighting or palette).
_STYLE_REQUIRED_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("palette/color",
     ("palette", "color", "colour", "warm", "cool", "pastel",
      "muted", "saturated", "tones", "hues", "earth tone")),
    ("technique/medium",
     ("watercolor", "watercolour", "ink", "line", "cel", "gouache",
      "crayon", "pencil", "vector", "painted", "hand-drawn",
      "illustrated", "illustration", "comic", "panel", "sketch",
      "brush", "wash", "anime", "studio")),
    ("anti-text",
     ("unmarked", "unbranded", "plain", "blank", "no text", "no logos",
      "no labels", "no markings", "without markings", "without text",
      "no signage", "no printed", "no inscribed")),
)

# Required signals in character_description. Same rule: every
# character_description must mention something from each list. Catches
# the "round-headed character with dot eyes" failure mode at config
# time, before a single render burns GPU credits.
_CHARACTER_REQUIRED_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("eyes",
     ("eye", "pupil", "iris", "lash", "lashes", "eyelash")),
    ("expression/mouth/brow",
     ("brow", "eyebrow", "mouth", "lip", "smile", "frown", "expression",
      "expressive", "articulated", "blush", "cheek")),
    ("clothing/body",
     ("shirt", "tee", "blouse", "sweater", "jacket", "dress", "jeans",
      "trousers", "skirt", "hoodie", "cardigan", "sari", "kurta",
      "dhoti", "kit", "jersey", "wears", "wearing")),
)


def _word_count(text: str | None) -> int:
    return len((text or "").split())


def _missing_required_signals(
    text: str | None,
    required: tuple[tuple[str, tuple[str, ...]], ...],
) -> list[str]:
    """Return the labels whose substring-list isn't matched by ``text``."""
    lowered = (text or "").lower()
    missing: list[str] = []
    for label, substrings in required:
        if not any(s in lowered for s in substrings):
            missing.append(label)
    return missing


def _assert_channel_prompt_richness(
    *,
    style_prefix: str | None,
    cast_narrator_desc: str | None,
    narrator_visual_mode: str,
) -> None:
    """Raise loudly when a channel's style/character configuration is
    too thin for Z-Image-Turbo / FLUX.2 to produce non-clip-art output.

    The assertion runs once per render at author_beat_prompts entry,
    BEFORE any LLM token is spent. Failure mode is informative:
    the error lists exactly which channel-YAML field is underweight
    AND which token classes are missing, so the operator can fix the
    channel config and re-run.

    Disabled when ``YTFACTORY_DISABLE_RICHNESS_GATE=1`` (debug renders
    + legacy test fixtures).

    Args:
        style_prefix: The channel/variant YAML's ``image_style_prefix``
            block as passed into author_beat_prompts.
        cast_narrator_desc: Resolved narrator description (cast.json's
            narrator.description OR channel YAML's
            ``character_description`` fallback).
        narrator_visual_mode: ``"on_screen"`` channels need a
            character_description; ``"voice_only"`` channels (sports
            channels with broadcast footage) don't render the narrator
            as a person and skip the character check.

    Raises:
        ValueError: with a multi-line error showing word counts AND
            missing token classes for every failing field.
    """
    if _env_flag_enabled("YTFACTORY_DISABLE_RICHNESS_GATE"):
        return

    failures: list[str] = []

    style_words = _word_count(style_prefix)
    if style_words < _STYLE_PREFIX_MIN_WORDS:
        failures.append(
            f"image_style_prefix is {style_words} words, "
            f"floor is {_STYLE_PREFIX_MIN_WORDS}. Under-prompted "
            f"styles render as flat clip-art (job 3cd2b3b5 floating-"
            f"objects post-mortem)."
        )
    missing_style = _missing_required_signals(
        style_prefix, _STYLE_REQUIRED_SIGNALS,
    )
    if missing_style:
        failures.append(
            f"image_style_prefix is missing required token classes: "
            f"{missing_style!r}. Every channel style must specify a "
            f"palette, a technique/medium, and a positive-presence "
            f"anti-text clause (Z-Image-Turbo has no negative-prompt "
            f"support)."
        )

    # Character check applies only to channels where the narrator
    # appears on screen. Sports / cosmosdecoded / footage-only channels
    # set narrator_visual_mode="voice_only" and don't need it.
    if narrator_visual_mode != "voice_only":
        char_words = _word_count(cast_narrator_desc)
        if char_words < _CHARACTER_DESCRIPTION_MIN_WORDS:
            failures.append(
                f"character_description is {char_words} words, floor "
                f"is {_CHARACTER_DESCRIPTION_MIN_WORDS}. Thin character "
                f"locks produce 'round-headed dot-eyed cartoon shapes' "
                f"with no face — diffusion model fills in interchangeable "
                f"clip-art figures."
            )
        missing_char = _missing_required_signals(
            cast_narrator_desc, _CHARACTER_REQUIRED_SIGNALS,
        )
        if missing_char:
            failures.append(
                f"character_description is missing required token "
                f"classes: {missing_char!r}. Every on-screen narrator "
                f"description must specify eyes (pupils/iris/lashes), "
                f"an expression mechanism (brow/mouth/blush), and "
                f"clothing (shirt/tee/jeans/etc.) so the diffusion "
                f"model renders a real face, not a default avatar."
            )

    if failures:
        msg = (
            "Channel prompt-richness gate failed — fix the channel "
            "YAML and re-run. Disable temporarily with "
            "YTFACTORY_DISABLE_RICHNESS_GATE=1 (not recommended in "
            "production):\n  - " + "\n  - ".join(failures)
        )
        raise ValueError(msg)


@_obs.traced("llm.prompts.author_beat_prompts", category="llm")
def author_beat_prompts(
    *,
    narration: str,
    beats: list[Beat],
    source_story: str,
    cast_narrator_desc: str | None,
    cast_default_emotion: str | None = None,
    style_prefix: str,
    opening_directives: dict | None,
    out_path: Path,
    narrator_visual_mode: str = "on_screen",
    supporting: list[dict] | None = None,
    ranks: list[dict] | None = None,
    era_anchor_prefix: str | None = None,
    mood: str | None = None,
    channel_key: str | None = None,
) -> list[dict]:
    """Author per-beat image prompts via the claude CLI and cache to
    ``out_path`` (typically ``data/cache/<slug>/prompts.json``).

    Returns the validated, schema-conformant list. Raises on LLM
    output that doesn't match beat count or schema (caller should
    fall through to the heuristic — make_shorts already handles None
    via images.load_prompts).

    Refiner pre-step (2026-05-14)
    -----------------------------

    When the env flag ``YTFACTORY_PROMPT_REFINER=1`` is set, this
    function runs an extra LLM call AFTER the existing
    :func:`_validate_and_clean` pass via :func:`_maybe_refine_prompts`.
    The refiner emits structured ``{refined_visual, refined_scene,
    style_block}`` fields per beat which are merged into each cached
    beat dict. Render-time consumers
    (:func:`pipeline.images.images.build_full_prompt`) pick those up
    when the same flag is set at render time.

    The new kwargs ``era_anchor_prefix``, ``mood``, ``channel_key`` are
    passed through to the refiner as informational context (the
    refiner never owns era_anchor or character_description — those are
    code-prepended at render time). They are optional; the legacy
    caller signature still works.
    """
    # Channel-richness pre-call assertion gate (post-2026-05-17). Under-
    # prompting was the root cause of the clip-art / floating-objects
    # render on job 3cd2b3b5: the AITA channel YAML's image_style_prefix
    # was 40 words ("Flat 2D crayon-style children's drawing…") and
    # character_description was 33 words ("simple dot eyes, a tiny line
    # nose"). Z-Image-Turbo + FLUX.2 prompting guides cite 80-250 words
    # of structured detail as the sweet spot; under that and the
    # diffusion model collapses to its seamless-backdrop product-photo
    # default. This gate enforces the floor at the architecture level
    # so every channel either ships rich prompts or fails loudly.
    _assert_channel_prompt_richness(
        style_prefix=style_prefix,
        cast_narrator_desc=cast_narrator_desc,
        narrator_visual_mode=narrator_visual_mode,
    )

    user_prompt = _build_user_prompt(
        narration=narration,
        beats=beats,
        source_story=source_story,
        cast_narrator_desc=cast_narrator_desc,
        cast_default_emotion=cast_default_emotion,
        style_prefix=style_prefix,
        opening_directives=opening_directives,
        narrator_visual_mode=narrator_visual_mode,
        supporting=supporting,
        ranks=ranks,
    )

    full_prompt = _SYSTEM + "\n\n---\n\n" + user_prompt

    # Strict structured output via wrapper-object schema (post-2026-05-16).
    # OpenAI/Azure structured outputs reject root-array types, so we wrap
    # the array in ``{"beats": [...]}`` and let _validate_and_clean's
    # Shape-A unwrap path turn it back into a list. Passing
    # ``strict_schema=True`` flips Azure's ``response_format.strict`` flag
    # to enforce the schema at the token-generation level (CFG engine),
    # not just nudge via response_format=json_object. Without strict mode
    # gpt-5.3-chat collapsed 14 beats into a single dict (Shape C,
    # unrecoverable) and the worker silently fell back to bare narration
    # text as image prompts — the floating-objects bug.
    full_prompt += (
        f"\n\nReturn EXACTLY {len(beats)} beat objects inside the "
        f"``\"beats\"`` array, in beat order. No prose, no markdown, "
        f"no commentary."
    )

    print(f"[prompts] authoring {len(beats)} beat prompts via claude CLI…")
    prompts_model = llm.model_for("prompts")
    raw = llm.call_claude_cli(
        full_prompt,
        output_json=True,
        json_schema=_BEAT_RESPONSE_SCHEMA,
        strict_schema=True,
        model=prompts_model,
        stage="prompts",
    )
    try:
        _obs.track_io(
            "llm.module.prompts",
            category="llm",
            input_text=full_prompt,
            output_text=raw,
            metadata={"stage": "prompts", "model": prompts_model},
        )
    except Exception:  # noqa: BLE001
        pass

    cleaned = _validate_and_clean(
        raw, beats, opening_directives,
        cast_narrator_desc=cast_narrator_desc,
    )

    # Optional refiner pass — Z-Image-Turbo DALL-E 3 playbook (rewritten
    # 2026-05-23 from the prior FLUX.2 [klein] calibration, see
    # pipeline/images/prompt_refiner.py:80 REFINER_VERSION='v2-zturbo').
    # Behind a feature flag so the canary lands without surprising
    # production renders. Pure additive — even if the refiner LLM fails,
    # the cleaned beats are untouched and the renderer falls back.
    cleaned = _maybe_refine_prompts(
        cleaned,
        era_anchor_prefix=era_anchor_prefix,
        character_description=cast_narrator_desc,
        style=style_prefix,
        mood=mood,
        channel_key=channel_key,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(cleaned, f, indent=2)
    print(f"[prompts] wrote {out_path}")
    return cleaned


def _maybe_refine_prompts(
    cleaned: list[dict],
    *,
    era_anchor_prefix: str | None,
    character_description: str | None,
    style: str | None,
    mood: str | None,
    channel_key: str | None,
) -> list[dict]:
    """Optional second-pass refiner — gated by ``YTFACTORY_PROMPT_REFINER``.

    Calls :func:`pipeline.images.prompt_refiner.refine_prompts_batch`
    and merges the returned ``refined_visual / refined_scene /
    style_block / refined_version / refined_input_hash`` fields into
    each beat dict in-place. Beats whose refiner slot is empty
    (per-beat failure) are unmodified — the renderer will fall back to
    the legacy path for those beats only.

    Returns the same list it received (mutated). Never raises — every
    failure mode is converted to "no refined fields added" so the
    authoring path is robust.

    Read by:
    - :func:`pipeline.images.images.build_full_prompt` at render time
      (also gated by the same env flag — kill-switch path).
    """
    def _track_gate(
        *, attempted: bool, skipped_reason: str | None,
        refined_count: int, fallback_count: int,
    ) -> None:
        try:
            _obs.track(
                "prompts.refiner_gate",
                category="llm",
                success=True,
                metadata={
                    "refiner_attempted": attempted,
                    "refiner_skipped_reason": skipped_reason,
                    "refined_count": refined_count,
                    "fallback_count": fallback_count,
                },
            )
        except Exception:  # noqa: BLE001
            pass

    if not _env_flag_enabled("YTFACTORY_PROMPT_REFINER"):
        _track_gate(
            attempted=False, skipped_reason="env_disabled",
            refined_count=0, fallback_count=0,
        )
        return cleaned
    if not cleaned:
        _track_gate(
            attempted=False, skipped_reason="empty_batch",
            refined_count=0, fallback_count=0,
        )
        return cleaned

    # Local import keeps the LLM-prompts module light at import time
    # (the refiner module imports the heavy ``images`` module lazily
    # too — see prompt_refiner.refine_prompts_batch).
    from pipeline.images import prompt_refiner as _refiner  # noqa: PLC0415

    try:
        refined = _refiner.refine_prompts_batch(
            cleaned,
            era_anchor_prefix=era_anchor_prefix,
            character_description=character_description,
            style=style,
            mood=mood,
            channel_key=channel_key,
        )
    except Exception as exc:  # noqa: BLE001 — refiner is best-effort
        print(
            f"[prompts] refiner pre-step FAILED ({exc}); "
            "all beats fall back to legacy path"
        )
        _track_gate(
            attempted=True, skipped_reason="exception",
            refined_count=0, fallback_count=len(cleaned),
        )
        return cleaned

    if len(refined) != len(cleaned):
        print(
            f"[prompts] refiner returned {len(refined)} slots for "
            f"{len(cleaned)} beats; ignoring (whole batch falls back)"
        )
        _track_gate(
            attempted=True, skipped_reason="count_mismatch",
            refined_count=0, fallback_count=len(cleaned),
        )
        return cleaned

    refined_count = 0
    for beat, slot in zip(cleaned, refined):
        if slot:  # empty dict {} → per-beat fallback, leave beat untouched
            beat.update(slot)
            refined_count += 1
    fallback_count = len(cleaned) - refined_count
    _track_gate(
        attempted=True, skipped_reason=None,
        refined_count=refined_count, fallback_count=fallback_count,
    )
    print(
        f"[prompts] refiner: refined {refined_count}/{len(cleaned)} beats"
    )
    return cleaned


def _env_flag_enabled(name: str) -> bool:
    """Treat ``"1" / "true" / "yes" / "on"`` (case-insensitive) as
    enabled. Anything else (including unset) is disabled.

    Pulled out so tests can monkeypatch one place; matches the
    convention used by :mod:`pipeline.niche_specs` and other
    flag-gated features (see CLAUDE.md "Cloud-canonical writes
    never re-create laptop channel folders").
    """
    val = os.environ.get(name, "").strip().lower()
    return val in {"1", "true", "yes", "on"}
