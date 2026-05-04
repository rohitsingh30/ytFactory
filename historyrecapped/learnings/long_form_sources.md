---
name: History Recapped long-form footage source strategy — HD-with-dispute or PD-soft, pick by risk tolerance
description: archive.org PD films cap at 480p-720p (the underlying 16mm scans don't go higher). HD + 2hr WW2 footage realistically only exists on third-party "restoration" YouTube channels (DroneScapes, etc.) that claim derivative-restoration copyright. Use them — upload private/unlisted, dispute any ContentID claim citing PD source under 17 USC §105.
type: feedback
---

Footage selection for the long-form sleep mode is a real tradeoff. There is no source that delivers HD + 2hr + free + zero-risk simultaneously — the user has to drop one constraint.

**Why:** Investigated 2026-05-03 → 2026-05-04. archive.org's PD WW2 films (Capra "Why We Fight", Ford "Battle of Midway", USGOV training films) are explicitly public domain — but they all derive from 16mm film scans done in 1942-45, and the available MP4s top out at 480p (typical) or 720p (rare). Stretched to 1920×1080 the result is visibly soft. The 2hr v0 pilot was authored against archive.org PD sources, the user reviewed it, and rejected the visual quality as "not HD."

**The practical HD-2hr-free path:** YouTube channels that re-upload + restore PD source — DroneScapes, Old Videos Restored, War Stories — provide 1080p (sometimes 4K) compilations of the same underlying material. Their cuts and color grades are derivative works they claim copyright on. ContentID risk is medium-high. **But** the underlying footage is genuinely PD, and ContentID disputes citing 17 USC §105 (federal employees in scope of duty produce no copyrightable work) succeed maybe 70-80% of the time.

**How to apply:**

1. **Pick by risk tolerance:**
   - **PD + soft (480p-720p) + zero risk:** archive.org. Acceptable for short pilots and channels where any ContentID friction is unacceptable.
   - **HD (1080p+) + 2hr + dispute fallback:** YouTube re-uploaders (DroneScapes, OVR). Use only when uploading private/unlisted FIRST, watching for ContentID, disputing if matched.
   - **HD + zero risk:** paid stock (Pond5, Storyblocks, Critical Past). $50-300 for 2hrs of cleared HD.

2. **Production default for History Recapped long-form (2026-05-04 decision):** DroneScapes-class 1080p HD + dispute fallback. Source v0.1 episode (Pacific War 1941-42) used `https://www.youtube.com/watch?v=RxcnJNnOiaw` — DroneScapes "WWII IN COLOR: Pacific Air War & Carriers", 1080p, 2hr 6min, ~1 GB MP4. Cached at `historyrecapped/footage/long_sources/RxcnJNnOiaw.mp4`.

3. **Dispute playbook when ContentID hits:**
   - Upload as **private** or **unlisted** initially
   - Wait 24 hours for ContentID scan
   - If matched → file dispute via Studio → Content → click the claimed video → "DISPUTE" → reason: "Public domain content"
   - In the explanation field paste: "The underlying footage in this video is United States government work (US Navy / US Army Air Forces, 1941-1945) and is in the public domain under 17 U.S.C. § 105. The claimant's restoration/colorization is a derivative work that does not add new copyright to the public-domain source under Bridgeman Art Library v. Corel Corp. (S.D.N.Y. 1999) and similar precedents."
   - Wait 30 days for the claimant to respond. If they don't respond or release the claim, video is cleared. If they reject the dispute, video stays up but monetization may route to them.
   - **Never** upload public + face-claim, because that surfaces a copyright strike risk if the claimant escalates from a Content ID claim to a manual takedown.

4. **Always mute the source audio** (`-an` in ffmpeg trim or `audio_source_mix: 0.0` in YAML). Their 1940s narration + period scoring is the part most likely to be matched by ContentID even if the video frames pass — and we don't want it under our soft narration anyway.

5. **Approved source pools, by tier:**
   - **HD + dispute risk** (preferred for production): DroneScapes (Pacific Air War 2hr, European Theatre 2hr), Old Videos Restored (4K Pearl Harbor 9 min, etc.), Periscope Film YouTube channel
   - **PD + soft** (use for low-risk pilots only): archive.org Capra "Why We Fight" series I-VII, Ford "Battle of Midway", "The Memphis Belle", "The Fighting Lady", "Victory at Sea", US National Archives YouTube channel `@usnationalarchives`, DVIDS
   - **Paid HD** (only if budget allows zero-risk): Pond5, Storyblocks, Critical Past

6. **What we do NOT use:** any source where the visible content includes modern presenters, on-screen text overlays, watermarks, modern graphic packages, or anything that looks like a 21st-century documentary cut. The footage must read as continuous archival period material with no modern editorial overlay (the only modern thing on screen is the result of the source's color grade).
