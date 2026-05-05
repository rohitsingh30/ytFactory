# Published catalog — every video shipped, by channel

Source of truth = `data/research/youtube/<account>.json` (one fetch per OAuth-
authenticated upload account, written by `pipeline/youtube_stats.py:fetch_all`).
Refresh with:

```bash
.venv/bin/python -c "from pipeline.youtube_stats import fetch_all; fetch_all()"
```

**Last refreshed: 2026-05-04** (28 videos across 4 active channels;
`airecap` and `rhymetimejunction` not yet OAuth-wired so cache is empty).

**Use this list before authoring any new script.** If a topic, angle, or
hook overlaps something already shipped, either pivot or write an explicit
sequel/companion that does not duplicate. We have one example of accidental
overlap: a draft long-form `pacific-war-1941-1945-sleep.json` was authored
2026-05-04 evening — but the channel had already shipped a 45-min long-form
covering Pearl Harbor → Midway earlier the same day (`fA1RCQYy5mU`). The
draft was retargeted to "Midway to Tokyo Bay · 1942–1945" instead of being
shipped as written. **That kind of duplicate effort is the bug this doc
exists to prevent.**

---

## historyrecapped — `HistoryRecapped` (7 subs, 2,352 total views, 13 active videos)

OAuth account: `historyrecapped` · channel: `@HistoryRecapped`
(`rsinghtomar54@gmail.com`). Publishes BOTH 50–60s Shorts AND 60–120 min
long-form sleep docs to the same channel.

### Long-form sleep (1 shipped)

| Date | ID | Duration | Title |
|---|---|---|---|
| 2026-05-04 | `fA1RCQYy5mU` | 45 min (2,681s) | The Pacific War: Pearl Harbor to Midway · 1941–1942 · 45-Minute Sleep Documentary |

In-flight long-form renders (cache exists, not yet uploaded):
- `western-front-1914-1918-sleep` — WWI, ~92 min, mp4 cached at `historyrecapped/shorts/western-front-1914-1918-sleep.mp4`, currently re-rendering with caption + trim fixes (PID 6561 as of 2026-05-04 21:38 PT).
- `italian-campaign-1943-1945-sleep` — WWII Italy, narration authored, 180 TTS chunks cached, narration.wav not yet assembled.

### Shorts (12 shipped, all 2026-05-03)

| ID | Title | Topic |
|---|---|---|
| `bc7lopoWSY4` | The 2,937 men who saved Britain | Battle of Britain (RAF, 1940) |
| `qqOU32B4p3Q` | The most impossible mission of D-Day | Pointe-du-Hoc Rangers, 1944 |
| `icUNo0DQudw` | The 9 days that saved the British army | Dunkirk evacuation, 1940 |
| `3n_6oUFWZMo` | What really happened on Omaha Beach | Omaha Beach, D-Day 1944 |
| `NfaaobtwC_A` | What really happened at Pearl Harbor | Pearl Harbor, 7 Dec 1941 |
| `W8_h-1UeGIE` | The five minutes that won the Pacific war | Battle of Midway, June 1942 |
| `Nk3YMq0BE2Y` | The one word that saved Bastogne | Bastogne ("Nuts!"), Bulge 1944 |
| `Ae-GM9kaiJE` | The largest tank battle in history | Kursk, July 1943 |
| `OaVNwb01VlQ` | How the Red Army trapped 300,000 Germans at Stalingrad | Stalingrad, 1942–43 |
| `r_Mrold1DNg` | The flag over the Reichstag — what really happened | Reichstag flag, May 1945 |
| `7OgyVExGvXI` | Why Arnhem was a bridge too far | Operation Market-Garden, 1944 |
| `nk3NU3dL6RM` | The first ground Japan ever lost | Guadalcanal, Aug 1942 – Feb 1943 |

Plus one private legacy upload `gVtihlYw9CQ` ("Sacred Games Trailer", 2018-08-15) — pre-channel-launch, not part of the active catalog.

### Topics already covered — DO NOT duplicate

- Battle of Britain (Short) — long-form remains untouched
- D-Day Pointe-du-Hoc + Omaha Beach (Shorts) — long-form D-Day-to-V-E-Day untouched
- Dunkirk (Short)
- Pearl Harbor (Short) + Pearl-Harbor-to-Midway (long-form 45 min)
- Battle of Midway (Short + long-form)
- Bastogne / Battle of the Bulge (Short)
- Kursk (Short)
- Stalingrad (Short)
- Reichstag fall of Berlin (Short)
- Operation Market-Garden / Arnhem (Short)
- Guadalcanal (Short)

### Untouched, high-EV next topics

- **Eastern Front WW2 (1941–1945) long-form** — Barbarossa → Stalingrad → Kursk → Bagration → Berlin. Sleep-niche gold; only the Stalingrad/Kursk/Reichstag moments touched as Shorts.
- **Pacific War: Midway to Tokyo Bay (1942–1945) long-form** — natural sequel to `fA1RCQYy5mU`. Guadalcanal → island stairs → Leyte → Iwo → Okinawa → Hiroshima → surrender. (My drafted narration can be retargeted to this.)
- **The Battle of the Atlantic (1939–1945) long-form** — U-boat war. No Shorts overlap.
- **D-Day to V-E Day Western Front (1944–1945) long-form** — Normandy to Berlin. Only specific moments shipped as Shorts.
- **North Africa Desert War (1940–1943) long-form** — Wavell / Rommel / Eighth Army. Untouched.
- **Burma Campaign (1942–1945) long-form** — Slim's 14th Army. Untouched.
- **First World War — Eastern Front, Italian Front, Gallipoli, Mesopotamia** — Western Front long-form already in flight; the other WW1 fronts are open.

---

## hindutavaanimated — `HindutavaJourney` (6 subs, 526 views, 4 videos)

OAuth account: `hindutavaanimated` · `rohit30.iitkgp@gmail.com`. All Shorts in Hindi (Devanagari).

| Date | ID | Duration | Episode |
|---|---|---|---|
| 2026-05-03 | `Tf92NmOA0lw` | 43s | अभिमन्यु का चक्रव्यूह — Abhimanyu's Chakravyuh |
| 2026-05-03 | `nIEq7zKUg9E` | 45s | संजीवनी पर्वत — Hanuman lifts the Sanjivani mountain |
| 2026-05-03 | `-JXKUhb39tE` | 62s | एकलव्य की गुरु-दक्षिणा — Eklavya's thumb |
| 2026-05-03 | `z1x_0cIa2nU` | 55s | कर्ण का कवच-कुंडल — Karna's armor |

Untouched Mahabharata/Ramayana arcs (high-EV):
- Bhishma's bed of arrows (शरशय्या)
- Drona's death (the lie about Ashwatthama)
- Krishna's Govardhan parvat
- Ravana's ten heads / Lanka war
- Sita's agni-pariksha
- Lakshmana-Indrajit duel
- Yudhishthira's swarga-arohan (the dog)

---

## mystoriesanimated — `My Stories Animated` (8 subs, 942 views, 7 videos)

OAuth account: `mystoriesanimated`. AITA / Reddit story Shorts. Pulls from r/AmItheAsshole, r/tifu.

| Date | ID | Title |
|---|---|---|
| 2026-05-01 | `EeO0M7Y5v60` | AITA for refusing a home birth in my living room? (private — pre-format-fix) |
| 2026-05-01 | `HHtMxhnsNVE` | AITA for Refusing to Invite Grandma to My Wedding Even If All 3 Kids Boycott? |
| 2026-05-01 | `weTiXP3rEgM` | He filed for custody while we still lived together… |
| 2026-05-02 | `ntjacxNrNDM` | I told my daughter I was DISGUSTED with her... AITA? |
| 2026-05-02 | `fkgHWNOzMlk` | AITA for not waking my jobless roommate up? |
| 2026-05-03 | `vQ8WX0-7-B0` | AITA for not letting my nephew touch my PS5? |
| 2026-05-03 | `5IfG6nA7Ngc` | AITA for refusing to host Thanksgiving after THIS? |

Each AITA story is unique by Reddit post id — check `mystoriesanimated/raw/`
for the slug-mapped post hash before re-pulling. The `aita-for-ruining-the-cake-j-*`
files in narrations/ are exploratory variants (cooking-format experiment) that
were not all shipped.

---

## sportstoriesanimated — `AnimatedSportsStories` (5 subs, 3,115 views, 3 videos)

OAuth account: `sportstoriesanimated`. Football moment recaps with real broadcast cut-in.

| Date | ID | Title | Moment |
|---|---|---|---|
| 2026-05-02 | `D3Y5-8jExIQ` | The 30 seconds that ended 44 years of waiting | Aguero 93:20, Man City title 2012 |
| 2026-05-03 | `WQep72LMSsU` | The goal that ended 76 years of waiting | Iniesta 2010 World Cup final |
| 2026-05-03 | `e2EVteSItkE` | Top 3 stoppage-time goals that rewrote football | Ranked compilation |

Untouched iconic football moments (wide open):
- Maradona — Hand of God / Goal of the Century 1986
- Zidane headbutt 2006 final
- Solskjaer 1999 Champions League final
- Liverpool Istanbul comeback 2005
- Greece Euro 2004 / Leicester 2016 (underdog runs)
- Beckham vs Greece 2001 free kick
- Ronaldo (Brazil) 2002 World Cup
- Klose 2014 vs Brazil 7-1
- Suárez handball 2010 vs Ghana

---

## airecap — empty (no OAuth, no shipped video)

Channel scaffolded 2026-05-03; X-first AI/tech daily news recap. Not yet shipping. Needs auth + ai_news scraper wiring.

## rhymetimejunction — empty (no OAuth, no shipped video)

Channel in pilot. One narration drafted (`hathi-raja-kahan-chale.json`), nothing rendered or uploaded.

---

## Refresh procedure

```bash
# Refresh every authenticated channel's cache (writes data/research/youtube/<account>.json)
.venv/bin/python -c "from pipeline.youtube_stats import fetch_all; fetch_all()"

# Then regenerate this doc by re-running the catalog dump (no automated regen
# script exists yet — the inventory is hand-maintained for now). When making
# changes, also update the memory mirror at:
#   ~/.claude/projects/-Users-rohit-ytFactory/memory/reference_published_catalog.md
```

If a channel returns "auth failed", the OAuth token has expired or never
existed for that account. See `reference_oauth_setup.md` (memory) and
`pipeline/auth.py` for the refresh flow.
