---
name: Footage windows must include buildup, not just the goal moment
description: When pinning sports-footage in_s/out_s, the window MUST include the buildup (the cross / pass / setup) before the climax. Starting at the goal moment means the viewer just sees celebration with no story context.
type: feedback
originSessionId: 27f7620f-11ff-49f6-9f72-bc12780d01eb
---
Rule: a sports-footage window of [in_s, out_s] must capture three beats in order: BUILDUP (the play developing) → CLIMAX (ball into net) → first 1-2s of CELEBRATION. Starting `in_s` at the climax cuts the buildup and the viewer reads it as "just a celebration clip" with no narrative.

**Why:** Top3-stoppage-goals v2 had Solskjaer in_s=34 and Iniesta in_s=14 — both started at the goal-mouth scramble / strike, missing Beckham's corner approach and the buildup pass to Iniesta. User feedback: *"only Ramos clip looks complete, the other two just mainly show the celebration but not the build up."* v3 pulled the windows back to in_s=29 (Beckham at the corner flag) and in_s=8 (Stamford Bridge wide approach) — both then showed the full goal sequence.

**How to apply:**
1. Sample the source clip at 1fps (NOT 0.5fps — too coarse). `ffmpeg -i src.mp4 -vf fps=1,scale=320:-1 sample_%02d.jpg`.
2. Read every frame. Identify by eye:
   - The frame where the buildup STARTS (player approaches corner / pass is played to the eventual scorer / cross is delivered).
   - The frame where the GOAL happens (ball crosses the line).
   - The frame where peak CELEBRATION is.
3. Set `in_s` ~1s BEFORE the buildup-start frame. Set `out_s` ~1-2s AFTER the goal frame. 8-10s windows tend to land it.
4. After committing in_s/out_s, sample frames inside the window to verify all three beats are present BEFORE re-rendering. Show the user one frame per phase if confirming the window.
5. Existing related memory: `feedback_sports_footage_cut_at_pivot.md` (`match_text` is the buildup beat, not the punchline — same principle, narration side).
