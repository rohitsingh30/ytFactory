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
import re
from pathlib import Path

from . import images, llm
from .beats import Beat


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


# Few-shot example: the hand-authored aita02 prompts.json. This is the
# canonical schema reference and demonstrates the right granularity
# (one specific noun phrase as key_visual, one spatial/contextual scene).
_FEWSHOT = [
    {
        "narration_beat": "I refused to split the bill",
        "key_visual": "an open-mouthed surprised face with raised eyebrows",
        "scene": "the character at a restaurant table holding a folded paper bill",
    },
    {
        "narration_beat": "I had a fourteen dollar salad",
        "key_visual": "a tiny green salad bowl with a single fork",
        "scene": "alone on a wooden table",
    },
    {
        "narration_beat": "they had four hundred dollars of steak and wine",
        "key_visual": "a steaming brown steak with crisscross grill marks on a white plate",
        "scene": "at a restaurant table next to a knife and fork",
    },
    {
        "narration_beat": "and three bottles of wine",
        "key_visual": "three tall wine bottles standing in a row",
        "scene": "on a wooden table at a restaurant",
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
1. NEVER include words asking for legible in-image text: label,
   sign, signs, signage, text, writing, written, letters, lettering,
   logo, brand, dollar sign, words, title, title card, subtitle,
   caption, menu, newspaper, headline, billboard, poster, speech
   bubble with words, phone screen with text, text message,
   notification, store name, license plate, name tag.
   Diffusion renders these as gibberish. Show the OBJECT (a phone
   screen with a red angry-face emoji), not the TEXT (a phone screen
   showing "I hate you").

   PROP CATEGORIES that almost always render garbled inscribed text
   if named directly: invitation, certificate, diploma, contract,
   prescription, receipt, greeting card, business card, boarding pass,
   ticket. Avoid the bare noun. Describe these props by SHAPE/COLOR/
   CONTEXT only — e.g. "a folded cream paper with a gold border on
   the table" not "a wedding invitation". Never use "X reading Y",
   "X that says Y", "X displaying Y" — the diffusion model will
   hallucinate the Y as melted letters.

2. ONE main subject per beat. Never "three friends", "several
   people", "a group of customers", "five kids". If the narration
   names multiple people, focus on ONE (the narrator alone, or just
   the object they're talking about).

3. The "scene" field MUST stay under ~40 words total. Attention
   dilutes past that on diffusion encoders.

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

Return ONLY the JSON array. No prose, no markdown fences.
"""


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

Return a JSON array of EXACTLY {len(beats)} objects, one per beat,
in order, each with these fields:

- "narration_line": the EXACT beat text (verbatim, copied from the
  BEATS list above) that this prompt is illustrating. The renderer
  uses this to bind the image to the right spoken moment, so it MUST
  match the beat you are illustrating.
- "key_visual": one short noun phrase describing the focal subject.
- "scene": the full image-gen prompt body.
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


def _validate_and_clean(
    raw: list,
    beats: list[Beat],
    opening_directives: dict | None,
    cast_narrator_desc: str | None = None,
) -> list[dict]:
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
        cleaned.append({"key_visual": kv, "scene": sc, "narration_line": nl})

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
) -> list[dict]:
    """Author per-beat image prompts via the claude CLI and cache to
    ``out_path`` (typically ``data/cache/<slug>/prompts.json``).

    Returns the validated, schema-conformant list. Raises on LLM
    output that doesn't match beat count or schema (caller should
    fall through to the heuristic — make_shorts already handles None
    via images.load_prompts).
    """
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

    # No json_schema here — the CLI routes that through tool-use and
    # the actual structured output doesn't land in `result`. Prompt
    # instructions + the parser's array-extraction fallback handle it.
    full_prompt += (
        f"\n\nReturn ONLY a JSON array of exactly {len(beats)} objects, "
        "in beat order. No prose, no commentary, no markdown fences. "
        "Your entire response must be parseable as JSON starting with `[` "
        "and ending with `]`."
    )

    print(f"[prompts] authoring {len(beats)} beat prompts via claude CLI…")
    raw = llm.call_claude_cli(
        full_prompt,
        output_json=True,
        model=llm.model_for("prompts"),
    )

    cleaned = _validate_and_clean(
        raw, beats, opening_directives,
        cast_narrator_desc=cast_narrator_desc,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        json.dump(cleaned, f, indent=2)
    print(f"[prompts] wrote {out_path}")
    return cleaned
