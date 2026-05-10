# Music bed sourcing — cross-channel rules

Established 2026-05-08 during the SportsRecapped rivalry-recap v26–v31
music-fix loop. Applies to every channel with a background music bed
(sportsrecapped, historyrecapped sleep mode, hindutavaanimated kathaa,
mystoriesanimated AITA, …).

## Three rules

### 1. Verify peak/RMS BEFORE treating a file as music

**Don't trust filenames.** The `<channel>/cache/<slug>/music_bed.wav`
files in the repo are the OUTPUT of a procedural-ambient-fallback
helper (`pipeline/render/long_form.py::build_music_bed`), not real
music tracks. They peak at –47 dB / mean –53 dB — essentially
silent. Copying them to the channel's `branding/music/` and shipping
results in a Short with no audible bed (the rivalry-recap v26 bug).

**Pre-flight check:**

```bash
ffmpeg -y -hide_banner -i <candidate>.wav -t 5 \
  -af volumedetect -f null - 2>&1 | grep -E "max_volume|mean_volume"
```

| metric | production music | reject as music |
|---|---|---|
| max_volume | ≥ –6 dB | < –12 dB |
| mean_volume | –14 to –17 dB | < –25 dB |

Reject anything below the right column.

### 2. Tone match > volume tuning

Volume can always be tuned later. Tone has to match the channel
register at selection time. Wrong tone at any volume is wrong.

| channel register | use | avoid |
|---|---|---|
| sports recap (narrated, 50s, factual) | calm cinematic, piano + soft pad, reflective | dramatic orchestral, epic-trailer, percussive |
| sleep history | warm-firelight ambient, 20+ min loop, no movement | anything melodic |
| Hindu kathaa | tabla + sitar pad, no Western chords | piano, electronic |
| AITA / mystories | none — narrator is the show | any music (it competes with the punchline) |

The 2026-05-08 cycle: shipped Kevin MacLeod "Five Armies" (dramatic
orchestral) and got rejected at every volume the user tried (–10 / –16
/ –22 dB). Switched tone (Lightless Dawn → Deliberate Thought, calm
piano) at the same –22 dB and the user accepted on first listen.

### 3. Audition 4 candidates before commit

Music selection is taste-driven. Don't pick blind — present a menu.

**Workflow:**

```python
# 1. Download 4 candidates as 60s loudnorm'd preview WAVs
mkdir -p /tmp/music_options
for url in candidates:
    name = slugify(url)
    download(url, f"/tmp/{name}.mp3")
    ffmpeg(f"/tmp/{name}.mp3", "-t", "60",
           "-af", "loudnorm=I=-14:TP=-3",
           "-ar", "44100", "-ac", "2",
           "-c:a", "pcm_s16le",
           f"/tmp/music_options/{name}_preview.wav")

# 2. Tell user the paths so they can audition locally
# 3. AskUserQuestion with one-line vibe descriptions per option
# 4. Apply user's selection — copy to <channel>/branding/music/
# 5. Update CC-BY attribution in <channel>/variants/<v>.yaml
#    upload.description_template
# 6. Re-render
```

Production source for free music: **Kevin MacLeod's incompetech.com**
catalog is CC-BY 4.0 and reliably hosts direct mp3 URLs. Calm-cinematic
candidates that work for sports recap:

- **Deliberate Thought** — slow piano + soft pad (currently shipped)
- **Lightless Dawn** — dreamy piano + strings
- **Long Note Three** — pure sustained drone (most minimal)
- **Ossuary 5 - Rest** — atmospheric organ + low pad

URL pattern: `https://incompetech.com/music/royalty-free/mp3-royaltyfree/<Track%20Name>.mp3`
(spaces are %20).

## CC-BY 4.0 attribution

Required in the YouTube description for any Kevin MacLeod track:

```
🎵 Music: "<Track Name>" by Kevin MacLeod (incompetech.com)
   Licensed under Creative Commons: By Attribution 4.0
   https://creativecommons.org/licenses/by/4.0/
```

Bake into the channel YAML's `upload.description_template` so it
auto-includes on every upload.

## Volume curve (sports recap)

Final shipped attenuation (after `loudnorm=I=-14:TP=-3` normalize on
the music file):

- `-10 dB` → too loud (competes with narrator)
- `-16 dB` → still loud
- `-22 dB` → sits clearly under speech, audible as bed (current)
- `-28 dB` → barely audible
- `-32 dB` → effectively silent for most listeners

Sweet spot for narrated content: **–22 dB ± 2 dB**.

Add a 6 kHz low-pass before mixing — the narrator's intelligibility
band is centred above that, so cutting music high-end keeps speech
clarity.

## Reference

- Source-of-truth: this file (`docs/music_bed_sourcing.md`)
- Memory entry: `feedback_music_bed_sourcing.md`
- First case study: SportsRecapped rivalry-recap v26–v31 (see
  `sportsrecapped/learnings/rivalry_recap_format.md` § "v26–v32 changes")
