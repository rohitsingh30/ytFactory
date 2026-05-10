# era_lock has a Goldilocks zone

**Status:** ACTIVE rule for every animated channel using AI image gen.
**First surfaced:** 2026-05-08 (pompeii-79 v4 → v8 iteration).
**Owner:** `pipeline/render/shorts.py:842-866` (era_lock injection
from `narration.metadata.era_lock`).

---

## What it is

`metadata.era_lock` is a string in the per-slug narration JSON that
the renderer prepends to `image_style_prefix` before calling diffusion.
Its purpose: prevent anachronisms (no WW1 trench coats in a Roman
city Short).

Authoring window:

```jsonc
// historyrecapped/narrations/<slug>.json
{
  "narration": "...",
  "metadata": {
    "era_lock": "Roman empire, AD 79; everyone wears togas, tunics, Roman sandals; terracotta-tile rooftops; NO firearms, NO modern uniforms, NO trench-coats, NO bolt-action rifles, NO industrial machinery, NO cars, NO tanks, NO aircraft, NO modern clothing"
  }
}
```

## Goldilocks zone — too long vs too short

| State | Symptom | Cause |
|---|---|---|
| **Too long** (v4) | 80% of frames become "Roman harbor + togas + ships" — every beat collapses to one composition | Composition-token positives (`harbor with merchant ships under sail`, `oil lamps`, `parchment scrolls`, `bronze pottery and amphorae`) — diffusion latches onto every concrete noun. Per-beat scene gets drowned. |
| **Too short** (v5) | Diffusion falls back to its strongest "soldiers + ruined city" prior — produces WW1 trench-coat soldiers in a Roman setting | era anchor reduced to `"period-accurate dress and architecture only"`. "Period-accurate" is meaningless to diffusion. Negatives ignored by guidance-distilled FLUX schnell (`pipeline/images.py:803`). |
| **Just right** (v6+) | Era-correct (togas, tile roofs, no anachronisms) + per-beat scene drives composition | Era anchor + clothing tokens + roof material + SPECIFIC failure-mode negatives. Period locked, composition free. |

## Authoring template

The era_lock string SHOULD contain ONLY:

1. **Era anchor** (period name + year): `"Roman empire, AD 79"` or
   `"WWI Western Front, 1916"` or `"Mughal India, 1632"`
2. **Clothing tokens** (the SINGLE most era-defining visual): togas,
   tunics, Roman sandals; greatcoat + helmet; sherwani + turban
3. **Roof / wall material** (the next most era-defining visual):
   terracotta-tile rooftops; thatched cottages; sandstone domes
4. **Specific failure-mode negatives** — name the actual anachronisms
   diffusion tends to produce. Generic "no modern stuff" ignored.
   For Roman: `NO firearms, NO trench-coats, NO bolt-action rifles,
   NO helmets-with-spikes, NO modern clothing`. For WWI: `NO assault
   rifles, NO modern body armor, NO aircraft carriers`.

## What NOT to put in era_lock

- **Composition tokens** — harbor with ships, oil lamps and scrolls,
  forums and amphorae, marketplace stalls. These dictate what's IN
  every frame and collapse per-beat scene direction. Move composition
  to per-beat scene strings in `prompts.json`.
- **Vague positives** — "period-accurate", "historical", "authentic",
  "ancient". Diffusion can't parse abstract qualifiers.
- **Generic negatives without specific anachronism names** — "no
  modern stuff", "no anachronisms". Negatives are weak signals to
  guidance-distilled diffusion; they're only useful when they name
  the SPECIFIC token diffusion would otherwise inject.

## Verification before render

Before kicking a render, eyeball the era_lock against:

- Length: 100-200 chars typical. If >250, you're probably packing
  composition into it.
- Token count of positives vs negatives: should be roughly balanced
  (3-5 positives, 4-7 specific negatives).
- Negatives should NAME the failure mode, not generic-anachronism.

## Cross-channel applicability

This rule applies to every channel that runs AI image gen:

- **HistoryRecapped** (animated archival fallback) — primary user
- **HindutavaAnimated** — every kathaa-short uses era_lock for the
  appropriate scriptural era
- **MyStoriesAnimated** — modern era; era_lock is short ("contemporary
  America, 2020s; casual modern dress; suburban interiors; NO period
  costumes") but same Goldilocks rule applies
- **Cosmos Decoded** — footage-only, doesn't use era_lock
- **SportsRecapped** — has its own era_lock per match (kit_lock + era
  for vintage broadcasts); same rule

## Related

- `pipeline/render/shorts.py:842-866` — injection site
- `pipeline/images.py:803` — note about negatives being ignored by
  guidance-distilled providers
- Memory: `feedback_era_lock_goldilocks.md`
