---
name: History Recapped long-form hook template (novelistic, sensory-first)
description: First 90 seconds of every long-form sleep video must follow a 6-beat novelistic hook — sensory immersion before topic anchor, second-person identification, central question, myth-bust setup. Reverse-engineered from Sleepy Time History's "What Did Early Humans ACTUALLY Do All Day?" (2.1M views, 148K-sub channel).
type: feedback
---

Every long-form sleep narration must open with a **novelistic 90-second hook** — not a "Welcome back to History Recapped, today we're talking about X" lecture opener. Sleep-history viewers swipe away from documentary-voice intros within 8 seconds. They stay for prose that transports them.

**Why:** Reverse-engineered from Sleepy Time History (`youtube.com/@SleepyTimeHistory`, 148K subs, 230 videos, 2.1M views on the early-humans episode). Their hook structure is a tight 6-beat formula that calibrates the listener to the warm-narrator register and seeds the central question they'll answer over the next ~2 hours. Our current narration JSONs go straight into chronological exposition — the same opener that reads fine for a 50-second Short kills a 90-min sleep video at minute one.

**How to apply:** Author the first paragraph of every long-form narration JSON to hit these beats in order. Word counts assume our ~95-110 wpm Sarah-clone cadence. The whole hook should land in 80-100s of audio, ~140-180 words.

### The 6 beats

**1. "Imagine ..." sensory deprivation (10-15s, ~25 words).**
Open with the literal word *Imagine* and remove the listener's modern context piece by piece. No alarm. No phone. No traffic. No emails. No meetings. Negative space — strip away the present.

> *"Imagine waking up without an alarm. No phone buzzing on the nightstand. No traffic sounds filtering through the window. No schedule to check. No emails waiting. No meetings to prepare for."*

**2. Sensory transport, era unnamed (15-25s, ~40 words).**
Five-senses immersion in the period setting. Do not name the year. Do not name the topic. Light, smell, sound, touch, voice. Build the world before pinning the date.

> *"Instead, you open your eyes to the soft gray light of dawn filtering into a rock shelter. The air is cool and crisp, carrying the scent of wood smoke and earth. Somewhere nearby, you can hear the low murmur of voices as others in your group begin to stir. A baby cries briefly, then settles. Someone laughs. Outside, birds are calling to each other in the growing light."*

**3. Era anchor (10s, ~25 words).**
NOW pin the time. One sentence. Direct. The contrast with beat 2's sensory present-tense lands harder.

> *"This is how humans woke up for hundreds of thousands of years. This was morning. It's 40,000 years ago, give or take a few millennia."*

**4. Identification ("you ARE this person") (15-20s, ~35 words).**
Shift from observed-from-outside to inhabited. Tell them what they are. Anchor it biologically — same brain, same body, same capacity. The point is to make the listener feel the strangeness from the inside.

> *"You're a homo sapiens living somewhere in what we'd now call Europe or Africa or Asia. Anatomically, you're identical to modern humans — same brain size, same body structure, same capacity for language and thought and emotion. But your world, your daily reality, is so alien to ours that it's almost impossible to imagine."*

**5. Pose the central question (8-12s, ~20 words).**
The question the entire video answers. Make it concrete and second-person, not abstract.

> *"So here's the question that keeps archaeologists and anthropologists endlessly fascinated. What did you actually do all day?"*

**6. Set up the myth (15-20s, ~30 words).**
Name the cliché image the audience already carries. Promise to subvert it. This is the "what you think you know is wrong" hook that sells the runtime.

> *"See, we have this image burned into our minds from countless movies and documentaries. Cavemen, brutish and hairy, constantly running from saber-tooth tigers..."*

### Hook anti-patterns (kill on sight)

- ❌ "Welcome back to History Recapped" / "Hi everyone, today we're going to look at"
- ❌ Naming the topic in the first 10 seconds ("Today's video is about Pearl Harbor")
- ❌ Stating the year before the sensory beats land ("On December 7th, 1941, at 7:48 AM Hawaii time...")
- ❌ Listing what "we'll cover" — no roadmap promises, no "in this video we'll explore"
- ❌ "Did you know that..." — punchy Shorts-cadence; wakes the sleep audience
- ❌ Loud or rhetorical question to the camera ("Have you ever wondered...?")
- ❌ Music sting / SFX cue — pure narration only, soft into the bed

### Adaptation by topic

The 6-beat pattern applies to any era or topic. Just swap the sensory load:

- **Western Front 1914-1918 sleep**: open in a chalk dugout — wet wool, sandbag walls, distant artillery. Anchor "1916, somewhere in northern France." Identify "you're a private in the British Expeditionary Force." Question: "What does it actually look like to live in this trench, day after day, week after week?" Myth: "Hollywood gives us the over-the-top charge — the whistles, the bayonets, the smoke. But that was 1% of the war."
- **Italian Campaign 1943-1945 sleep**: open in an olive grove at dawn — lemons, dust, mule bells. Anchor "1943, somewhere in southern Italy." Identify "you're a sergeant in the U.S. Fifth Army." Question: "What is it actually like to fight your way up a country shaped like a boot?" Myth: "Patton and Bradley got the headlines from France. Italy was the campaign nobody could finish."

### What this replaces

This template **supersedes** any earlier "documentary-voice intro" pattern that may have shipped in v0 narrations (e.g. `western-front-1914-1918-sleep.json` first paragraph). When the hook on an existing narration JSON doesn't hit these beats, rewrite the first 150 words before re-rendering — the rest of the script is fine.
