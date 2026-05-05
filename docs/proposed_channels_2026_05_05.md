# 10 proposed YouTube channels for ytFactory — 2026-05-05

Research grounded in current 2025-2026 RPM/CPM data and live channel models. Hard-constrained to **footage-only** sourcing (NO AI image gen), F5-TTS-MLX (English) or Kokoro hf_alpha (Hindi), and the existing `render_footage_only.py` 16:9 + 9:16 paths. Every pick has a long-form anchor AND a Shorts variant so source material amortises across both.

## Executive ranking

If you ship three this quarter, ship in this order: **(1) AviationDisasterDecoded — NTSB reports + ICAO photos + airport b-roll, $8-15 RPM aviation/insurance ad bracket, near-zero ContentID risk, the Mentour Pilot lane is hungry for a faceless competitor; (2) BoardroomCollapse — retail + corporate bankruptcies via SEC filings + earnings clip pulls + storefront stock + newspaper archives, $10-22 RPM finance bracket, the Company Man / Modern MBA wedge; (3) ColdCaseChronicles — pre-1928 PD newspaper murders + Library of Congress photos + period maps, $5-8 ad RPM but the highest watch-time multiplier in the bunch and zero copyright surface area.**

The next four (FinanceCrashFiles, GeoStrategyMaps, FraudFiles, SleepyEarth) are strong but each carries one risk worth pricing in. The last three (LegalDocketed, SpaceMissionDecoded, MaritimeLost) are real opportunities but more competitive — ship them only after the top three prove the pipeline.

The goal of all 10 is **high-CPM categories (finance, legal, insurance, tech, real estate)** combined with **archive-rich niches** so the same source artefact powers a 30-min long-form AND a 50-60s Short.

---

## 1. AviationDisasterDecoded — `aviationdisasterdecoded`

**Format.** Long-form 25-40 min "what really happened" reconstructions. Shorts 50-60s on a single chilling cockpit moment. Cadence: 1 long-form/wk + 4 Shorts/wk.

**The hook.** Mentour Pilot wins because he's a real 737 captain explaining timeline-by-timeline — but he's one man, ships ~1/wk, and his videos run 30-50 min. The opening is **"this is the last 90 seconds of [flight number]"** with the actual cockpit voice recorder transcript scrolling, no fluff. A faceless competitor that ships 3x his cadence on the same NTSB/ICAO source pool can carve a real wedge. Every video closes with **"the recommendation that came out of this — and whether it actually got implemented"** which is the angle Mentour underplays.

**Source palette.** (a) NTSB Aviation Accident Database (free, PD, complete reports + photos); (b) BEA/AAIB/TSB (French, UK, Canadian counterparts — also PD); (c) FAA airport diagrams + Wikimedia Commons aircraft photography (CC-BY-SA); (d) Airport stock b-roll from Pexels/Pixabay/Storyblocks; (e) ATC.net + LiveATC.net audio archives (clearly licensed clips only — avoid YT re-uploaders). Crash-site photos: NTSB docket attachments are PD.

**RPM evidence.** Aviation lives in the tech/finance ad bracket because of insurance + airline + flight-school advertisers. Vehicle/transportation niches sit at $8-15 RPM per OutlierKit's 2026 data; Mentour Pilot's 2.4M-sub channel is reportedly grossing well into 6-figures monthly. Faceless RPM is typically 80-90% of personality-led in the same category.

**Why this fits ytFactory.** Pure footage_only path. Stills + b-roll + Ken Burns over NTSB photos is exactly what `render_footage_only.py --aspect 16:9` was built for. F5-TTS-MLX sarah voice carries the documentary tone we already validated on HistoryRecapped long-form.

**Risks.** Monetization-friendly but YouTube limits ad density on "fatal incident" videos — title carefully ("Why Air France 447 was unrecoverable" not "100 dead in plane crash"). Some recent disasters carry ContentID risk if a news org owns exclusive footage; stick to events 5+ years old where NTSB photos dominate.

**Anchor.** Long-form: *"The 31 Seconds That Doomed Air France 447 — A Decoder."* Shorts: *"This co-pilot pulled back on the stick the entire fall. Here's why."*

---

## 2. BoardroomCollapse — `boardroomcollapse`

**Format.** Long-form 22-30 min retail/corporate bankruptcy autopsies. Shorts 50-60s on a single fatal decision (e.g. "The day Sears bet against the internet"). Cadence: 1 long/wk + 3 Shorts/wk.

**The hook.** Company Man (Mike, 2.7M subs) does this well but ships ~2 long-forms/month and rarely does Shorts. Bright Sun Films does the same lane in a slower more cinematic register. The wedge is **document-first storytelling** — pull the actual SEC 10-K / Chapter 11 filing on screen, highlight the line that killed the company, then cut to the storefront photo. Visual signature = SEC EDGAR + earnings call transcript chyron + retail storefront b-roll.

**Source palette.** (a) SEC EDGAR (PD, every 10-K/8-K/Chapter 11 filing); (b) PACER court filings (paywalled but $0.10/page, cheap); (c) Newspapers.com Chronicling America for pre-1928 retail history (PD, free); (d) earnings call transcript audio from Seeking Alpha / Motley Fool when fair-use applicable; (e) storefront stock from Pexels + retail mall photos from Wikimedia + Library of Congress historic store photography.

**RPM evidence.** Personal Finance + Make Money Online + Business niches sit at $15-22 CPM per Virlo's 2026 RPM data, with creator RPM around $10-19. The Company Man lane sits squarely in this bracket — Modern MBA reportedly clears multiple six-figures/yr from a 1.4M sub base on this exact format.

**Why this fits ytFactory.** Reuses footage_only renderer + F5 sarah. The web-screenshot-into-Ken-Burns pattern is already in the pipeline (we use it for newspaper headlines on HistoryRecapped). Each long-form's pull-quotes become Shorts trivially — the source SEC filing + storefront photo + 30s narration ports straight to 9:16.

**Risks.** Earnings call audio fair-use is a grey area — keep clips ≤15s and add commentary. ContentID can flag specific TV news clips; substitute newspaper screenshots and storefront stock instead. Don't editorialise on living executives — defamation surface area.

**Anchor.** Long-form: *"How JCPenney Burned $4.9B — The Ron Johnson Memo Nobody Read."* Shorts: *"This one Sears decision, in 2002, killed the company. They had the Amazon deal."*

---

## 3. ColdCaseChronicles — `coldcasechronicles`

**Format.** Long-form 35-60 min sleep-mode cold cases (read like a documentary, paced for ambient listening) AND 50-60s Shorts hook with newspaper headline reveal. Cadence: 1 long/wk + 5 Shorts/wk.

**The hook.** True crime is saturated, but the **pre-1940 PD newspaper crime** lane is wide open because most channels chase recent cases that need bodycam / 911 audio (copyright minefield). Pull from Library of Congress *Chronicling America* (PD pre-1928 papers, fully indexed and downloadable). The visual signature is yellowed broadsheets, period photographs, and cursive handwritten coroner reports — instantly distinctive. Lazy Masquerade and That Chapter own the modern lane; nobody owns pre-1928.

**Source palette.** (a) Library of Congress *Chronicling America* (PD, ~3000 newspapers, OCR-searchable); (b) Newspapers.com (paywalled $20/mo but covers 1928-modern fair-use); (c) Find A Grave + state archives for victim photos; (d) Wikimedia Commons period photography; (e) Sanborn Fire Insurance Maps (PD, gorgeous period city maps); (f) National Archives (PD federal investigative files for some Mann Act / mail fraud cases).

**RPM evidence.** True crime AdSense RPM runs $4-8 per FluxNote's 2026 data — lower than finance, but cold-case content has 2-3x the watch-through and a long-form 50-min cold case routinely hits 8-15 min average view duration vs. 2-4 min for fast-cut crime, which mid-roll-stacks the revenue.

**Why this fits ytFactory.** Pure web-screenshot + still-photo + Ken Burns workflow. Reuses HistoryRecapped's `long_form_visual_signature` warm-firelight grade — newspaper sepia + cool-dawn photography looks _correct_ for the genre. Sleep-mode cadence is already validated on the channel.

**Risks.** Modern cases carry defamation + ContentID risk; staying pre-1940 sidesteps both. A "ripped-from-the-archives" rule is easier to enforce than a case-by-case legal review.

**Anchor.** Long-form: *"The 1922 Hall-Mills Murder: How a Pig Woman Cracked America's Most Famous Cold Case."* Shorts: *"In 1897, a woman vanished in Brooklyn. The case file was sealed for 60 years. Here's what was inside."*

---

## 4. FinanceCrashFiles — `financecrashfiles`

**Format.** Long-form 25-40 min financial crash / bubble / scandal autopsies (Enron, LTCM, Long-Term Capital, Wirecard, FTX, Archegos). Shorts 50-60s on the single trade or accounting trick. Cadence: 1 long/wk + 3 Shorts/wk.

**The hook.** Patrick Boyle (former hedge-fund manager) and Plain Bagel own this lane but both are face-on-camera and ship slowly. Coffeezilla owns crypto fraud in a face-led format. The faceless wedge is **document-led** — show the actual 8-K, the actual courtroom exhibit, the actual SEC enforcement order. Pair with a dispassionate F5 sarah voice and you have *Modern MBA for fraud*. Differentiator: a chapter for "what the auditor missed" is non-negotiable in every video.

**Source palette.** (a) SEC EDGAR + DOJ press releases (PD); (b) court filings via PACER + RECAP archive (free re-distribution); (c) Bloomberg / WSJ headlines as fair-use newspaper-style screenshots ≤8s on screen; (d) trading floor + skyscraper stock (Pexels + Storyblocks); (e) Wikimedia trader-portrait photos.

**RPM evidence.** Finance is the highest-paying YouTube category at $15-45 CPM per multiple 2026 sources, with creator RPM $10-25. Personal-finance / investing-education sub-niches lead. Coffeezilla's reported earnings exceed $1M/yr from this exact ad bracket.

**Why this fits ytFactory.** Same renderer, same sarah voice. The web-screenshot module already exists. Document-heavy stills are exactly the asset class footage_only handles best.

**Risks.** Defamation on living named figures is real — use SEC enforcement orders + court filings as the source of every claim, never editorialise. Stay away from conspiracies. Avoid specific-stock-pick framing or YouTube Finance category may demonetise.

**Anchor.** Long-form: *"Wirecard: How $2 Billion Vanished and Nobody at EY Asked the Right Question."* Shorts: *"This one Excel cell at Archegos meant Bill Hwang lost $20B in 48 hours. Here's the formula."*

---

## 5. GeoStrategyMaps — `geostrategymaps`

**Format.** Long-form 18-25 min geography/geopolitics explainers. Shorts 50-60s on a single weird border / chokepoint / rivalry. Cadence: 1 long/wk + 4 Shorts/wk.

**The hook.** RealLifeLore and Wendover dominate but ship slowly and cover broad topics; they ignore a huge backlog of niche geography. The wedge is **maritime + airspace + chokepoint specificity** — Suez, Bosphorus, Strait of Hormuz, Northwest Passage, Diego Garcia, Spratly Islands, Drake Passage. Visual signature: animated map + Wikimedia satellite imagery + naval/cargo b-roll. Each video answers "if this chokepoint closed for a week, what happens to your phone / your gas tank / your coffee."

**Source palette.** (a) Natural Earth + OpenStreetMap (PD) for map backgrounds — the user already has the rendering chain for stills; (b) Sentinel-2 + Landsat satellite imagery via NASA Worldview / Copernicus (PD, near-realtime); (c) Wikimedia Commons aerial + harbor + ship imagery (CC-BY-SA); (d) US Navy + USCG photo archives (PD); (e) maritime AIS data via MarineTraffic snapshots; (f) container terminal stock from Pexels/Storyblocks.

**RPM evidence.** Education + business cross-bracket — Wendover's reported sponsor floor is $20-30K per video, ad RPM in the $8-15 range. Same niche, same RPM bracket as RealLifeLore.

**Why this fits ytFactory.** Stills + Ken Burns + map zooms = exactly footage_only. F5 sarah's documentary register fits.

**Risks.** Saturation is real — RealLifeLore, Wendover, CaspianReport, Atlas Pro, Atlas Geographica, Polymatter all ship in this lane. Need a sharp angle (chokepoints + supply chains) and stay disciplined; do not become "10 weird borders" listicle filler. Geopolitical content can be demonetised during conflict spikes — keep the framing structural, not partisan.

**Anchor.** Long-form: *"The Bab-el-Mandeb: The 18-Mile Strait That Decides Whether Your iPhone Ships."* Shorts: *"95% of the world's chip exports cross this single 100-mile strait. Here's what a blockade would look like."*

---

## 6. FraudFiles — `fraudfiles`

**Format.** Long-form 22-35 min fraud / con / scheme breakdowns (Theranos, FTX, Madoff, Anna Delvey, Stockton-Rush OceanGate, Adam Neumann). Shorts 50-60s on the single deception. Cadence: 1 long/wk + 3 Shorts/wk.

**The hook.** Coffeezilla owns crypto fraud (face-led). Lemmino does deep dives but rarely. The wedge is **the legal-document timeline** — every claim is sourced to an SEC complaint, FBI affidavit, or court exhibit on screen. The differentiator from FinanceCrashFiles (#4) is this is *individual-led* fraud (a single con artist) vs. corporate / market collapse — different stories, same source backbone.

**Source palette.** (a) DOJ / FBI / SEC press releases + complaints (PD); (b) PACER + RECAP archive court filings; (c) ProPublica + ICIJ investigative document leaks (typically licensed for journalistic re-use); (d) Wikimedia portrait photos + Getty fair-use editorial; (e) suburban / luxury / tech-office stock (Pexels + Storyblocks); (f) trial sketch artwork via newspaper-archive scans.

**RPM evidence.** Same finance/legal $15-25 RPM bracket. Coffeezilla's revenue is the proof point.

**Why this fits ytFactory.** Exactly the same render path as #2 and #4. Once we build the SEC EDGAR + court-filing screenshot tooling for BoardroomCollapse, this channel reuses it.

**Risks.** Defamation on living subjects. Hard rule: every claim must trace to a charged or convicted defendant in court documents. Ad demonetisation around financial fraud is rare but possible — title carefully ("the document trail" not "criminal").

**Anchor.** Long-form: *"OceanGate: The 27 Engineering Letters That Predicted the Implosion."* Shorts: *"This one paragraph in Theranos's S-1 should have killed the IPO. The auditor signed anyway."*

---

## 7. SleepyEarth — `sleepyearth`

**Format.** Long-form 60-120 min sleep-mode geography + civilization meditations (Sahara, Amazon, Silk Road, the Mediterranean as a single organism). NO Shorts; this is anchor-only because the sleep niche doesn't reward Shorts. Cadence: 1 long/wk.

**The hook.** Sleepy Time History dominates the historical sleep niche; nobody owns the **Earth-as-subject** sleep niche where the chapter structure is biome / ocean / mountain range / desert across 90 minutes of soft narration. Same novelistic hook template HistoryRecapped already validated, applied to geography.

**Source palette.** (a) NASA Earth Observatory + USGS Landsat (PD); (b) Sentinel-2 ESA Copernicus (CC-BY-SA); (c) BBC Earth-style stock from Pexels + Pixabay (curated for slow majestic shots); (d) Wikimedia desert / forest / coastline photography; (e) NOAA climate b-roll (PD); (f) ambient field-recording audio from FreeSound CC0.

**RPM evidence.** Sleep / soundscape category sits at ~$10-11 RPM per FluxNote / OutlierKit 2026 — surprisingly high because advertisers consider sleep-niche faceless content "brand safe" and audio-bed-friendly for mid-roll. Average watch-time 20+ min.

**Why this fits ytFactory.** Direct port of the existing HistoryRecapped long-form path: warm-firelight grade, F5 sarah soft voice, ambient bed, no captions, 16:9. The render tooling (`long_form_trim_aspect_short_circuit`, `_trim_clip_letterbox` stream-copy) already optimised for this.

**Risks.** Sleep niche has a slow algorithmic on-ramp (3-6 months); revenue ramps later than other picks here. Mitigate by sharing audio-only versions to a podcast feed for compounding watch-time elsewhere.

**Anchor.** Long-form: *"The Sahara: 90 Minutes Across the Sea of Sand."* (No Shorts variant — this channel is long-form only by design.)

---

## 8. LegalDocketed — `legaldocketed`

**Format.** Long-form 18-30 min "what the court actually said" Supreme Court + appellate breakdowns. Shorts 50-60s on a single oral argument exchange. Cadence: 1 long/wk + 3 Shorts/wk.

**The hook.** LegalEagle (face-led, 3M subs) owns "lawyer reacts." SCOTUS Oral Argument Transcripts (faceless, niche) just plays audio. The wedge is **the case in 25 minutes with the briefs, the oral arguments, the opinion, AND the dissent on screen** — animated quote-pulls from the actual filings. Audience: law students, paralegals, policy nerds, citizens. Tone: dispassionate explainer, not advocacy.

**Source palette.** (a) supremecourt.gov oral argument audio + PDFs (PD); (b) PACER + CourtListener for appellate briefs (free re-distribution via RECAP); (c) Justia + Cornell LII for opinion text (PD); (d) Library of Congress photos of justices + courthouses; (e) C-SPAN PD federal hearings via archive.org PD reuploads (verify PD status case by case); (f) Wikimedia courthouse photography.

**RPM evidence.** Legal category $10-35 CPM per 2026 data — second only to finance. Personal-injury, immigration, and tax-attorney advertisers stack into the legal category.

**Why this fits ytFactory.** Same document-screenshot + still-photo + Ken Burns workflow. F5 sarah documentary tone is correct register.

**Risks.** This audience is more discerning — "what the court said" must actually be accurate or you get ratio'd. Lower competitive pressure than #5 but higher quality bar. Avoid hot-button cases for the first 50 videos; build credibility on procedural / business-law cases first.

**Anchor.** Long-form: *"Loper Bright vs. Raimondo: The 32-Page Footnote That Killed Chevron Deference."* Shorts: *"Justice Sotomayor asked this one question that ended the oral argument early. Here's the audio."*

---

## 9. SpaceMissionDecoded — `spacemissiondecoded`

**Format.** Long-form 25-40 min specific-mission deep-dives (Apollo 13, Voyager Golden Record, Space Shuttle Challenger O-ring, Mars Polar Lander, Cassini Grand Finale, Hubble's first mirror flaw, JWST commissioning). Shorts 50-60s on the single decision moment. Cadence: 1 long/2wk + 3 Shorts/wk.

**The hook.** Scott Manley owns this lane (face-led, slow cadence). Veritasium hits adjacent with broader physics. The wedge is **NASA mission timeline as a structured doc** with mission control loops, telemetry plots, engineering memos, and the post-flight investigation report all on screen. Every video resolves with "the engineering recommendation that came out of this and whether NASA implemented it."

**Source palette.** (a) NASA Image and Video Library (PD, fully open); (b) NASA Technical Reports Server NTRS (PD, every mission report); (c) National Archives Record Group 255 — NASA audiovisual holdings (PD); (d) JPL + JSC mission control footage (PD); (e) Smithsonian National Air & Space photos (mostly PD or CC); (f) ESA imagery (CC-BY-SA-IGO); (g) Wikimedia engineer portraits.

**RPM evidence.** Science / technology bracket $8-25 CPM per 2026 data. Tech advertisers (Brilliant, KiwiCo, Squarespace) sponsor this category heavily. Similar bracket to GeoStrategyMaps but with stronger sponsor pull.

**Why this fits ytFactory.** All-PD source pool means zero ContentID risk. Pure footage_only render. Animation budget required for orbit / trajectory diagrams — but we can use NASA's existing PD animations rather than build our own.

**Risks.** Saturation moderate. Need the engineering-memo angle to differentiate from Curious Droid, Primal Space, Astrum. Audience is technical and will catch errors quickly — quality bar is high.

**Anchor.** Long-form: *"The Last 13 Minutes of Mars Polar Lander — A Decoder."* Shorts: *"This single line of code in the lander's altimeter killed the mission. Here it is."*

---

## 10. MaritimeLost — `maritimelost`

**Format.** Long-form 22-40 min ship/shipwreck/maritime-disaster reconstructions (Lusitania, Estonia, El Faro, Sewol, modern container losses, naval collisions). Shorts 50-60s on the single moment of failure. Cadence: 1 long/wk + 3 Shorts/wk.

**The hook.** Oceanliner Designs (Mike Brady) owns historical ocean liners. Brick Immortar owns modern incidents but ships slowly and uses a stylised brick-aesthetic. The wedge is **post-mortem report-driven storytelling** with USCG / NTSB / IMO investigation reports on screen, plus the ship's deck plans and AIS replays for modern incidents.

**Source palette.** (a) USCG marine board reports + NTSB marine accident reports (PD); (b) MAIB UK / TSB Canada / BSU Germany counterparts (PD); (c) Wikimedia ship photography + Encyclopedia Titanica style fan archives (CC); (d) Library of Congress historic harbor photography; (e) MarineTraffic AIS replay screenshots; (f) NOAA + ESA satellite imagery for sea-state context; (g) PD newsreel via archive.org for pre-1960s incidents.

**RPM evidence.** Adjacent to aviation — insurance + maritime law + cruise advertisers. Similar $6-12 RPM bracket. Brick Immortar's sustained 800k+ subs on slow ship cadence proves audience exists.

**Why this fits ytFactory.** Same document-led footage_only path as #1. Once AviationDisasterDecoded's NTSB-report-screenshot tooling exists, MaritimeLost reuses it for marine boards.

**Risks.** Modern incidents (post-2000) have heavier ContentID exposure on news b-roll — stick to investigation-report stills + stock harbor footage. Some recent cases (e.g., MV Dali / Key Bridge collision 2024) have ongoing litigation; defer until reports are final.

**Anchor.** Long-form: *"The 26 Hours of El Faro: How a Cargo Ship Sailed Straight Into Hurricane Joaquin."* Shorts: *"This one bridge-team conversation, recovered from the wreck at 15,000 feet, explains how El Faro died."*

---

## Skip list — niches considered and rejected

- **Dashcam compilations** — copyright minefield (background music ContentID + driver identifiability + scraped from r/IdiotsInCars without licensing). Killed.
- **Reaction-style true crime (modern cases)** — saturated by Lazy Masquerade / Rotten Mango / Stephanie Soo + bodycam ContentID risk + defamation surface area on living subjects. ColdCaseChronicles' pre-1928 lane is the only safe wedge here.
- **Health / wellness / supplement** — high RPM but YouTube's medical-misinformation guidelines are razor-thin and one strike kills monetization. Skip unless we hire a clinician reviewer.
- **Crypto trading / "make money online"** — highest CPM but YouTube's Finance Verification gate (announced 2024) plus FTC scrutiny make this a compliance trap for a faceless channel without a real licensed advisor.
- **Reaction / commentary** — fair-use grey zone, ContentID risk, and not a good fit for a footage-only pipeline anyway.
- **Conspiracies / paranormal / cryptid** — easy to grow but YouTube periodically demonetises whole topic clusters and ad RPM is among the lowest. Skip.
- **Recipe / cooking faceless channels** — saturated, low RPM ($1-3), and footage requirements (overhead cooking shots) don't match our archive-first pipeline.
- **Animated-map "history" channels** — already crowded by Kings and Generals + Real Time History + Historia Civilis; we'd be the 50th competitor with no edge.
- **Celebrity gossip / drama** — defamation exposure + ContentID risk on entertainment news b-roll + brand-safe ad pull is poor.
- **Gaming + tech-product reviews** — saturated, requires capture cards / unboxing, doesn't fit footage-only sourcing.
- **Top-10 listicle generic channels** — we already have `/make-top10`; don't dilute it with a generic channel — keep listicle format reserved for the existing channels' niches.
- **Modern war news / OSINT conflict** — high views but the highest demonetisation risk on YouTube and ContentID hell on combat footage. The HistoryRecapped war-history lane (events 50+ years old) is the safe version of this.

---

## Cross-channel pipeline observations

Eight of the ten picks (#1, #2, #3, #4, #6, #8, #9, #10) share the same **document-screenshot + still-photo + stock-b-roll + Ken Burns** workflow. That means one well-built screenshot-and-Ken-Burns module pays off across 80% of the proposed catalog. Build it once for AviationDisasterDecoded and the marginal cost of the next seven channels collapses.

The remaining two (#5 GeoStrategyMaps, #7 SleepyEarth) need **map-rendering** (Natural Earth + Sentinel-2 satellite tiles). That's one shared module across both — also worth building once.

If the user wants to rank by *engineering leverage*, ship in this order to amortise the tooling: **(1) AviationDisasterDecoded → builds NTSB-report screenshot tool → (2) BoardroomCollapse reuses it → (4) FinanceCrashFiles + (6) FraudFiles + (8) LegalDocketed reuse it → (3) ColdCaseChronicles needs newspaper-OCR tool (build it) → (10) MaritimeLost reuses NTSB tool → (9) SpaceMissionDecoded reuses NASA-PD pull → (5) GeoStrategyMaps + (7) SleepyEarth need map-render tool.**

That's a clean 4-tooling-block dependency graph: NTSB-report screenshot, SEC-EDGAR screenshot, newspaper-OCR (Chronicling America), map-render. Build those four and the entire 10-channel slate becomes parametric.

---

## Sources

- [19 Most Profitable YouTube Niches 2026 — OutlierKit](https://outlierkit.com/blog/most-profitable-youtube-niches)
- [25 Highest RPM YouTube Niches in 2026 — Virlo](https://virlo.ai/blog/highest-rpm-niches-on-youtube)
- [YouTube CPM Overview 2026 — Upgrowth](https://upgrowth.in/youtube-cpm-overview-highest-paying-niches-2026/)
- [True Crime Shorts RPM 2026 — FluxNote](https://fluxnote.io/guides/youtube-shorts-rpm-true-crime-niche)
- [Faceless YouTube Niches 2026 — OutlierKit](https://outlierkit.com/resources/faceless-youtube-channels/)
- [Mentour Pilot channel](https://www.youtube.com/@MentourPilot)
- [NTSB Aviation Investigation Search](https://www.ntsb.gov/Pages/AviationQueryv2.aspx)
- [Modern MBA channel](https://www.youtube.com/@ModernMBA)
- [Company Man channel](https://www.youtube.com/@companyman114)
- [Bright Sun Films coverage of Toys R Us](http://crazyeddiethemotie.blogspot.com/2026/02/bright-sun-films-toys-r-us-2026-update.html)
- [Coffeezilla — Wikipedia](https://en.wikipedia.org/wiki/Coffeezilla)
- [Patrick Boyle YouTube channel](https://www.youtube.com/channel/UCASM0cgfkJxQ1ICmRilfHLw)
- [RealLifeLore channel](https://www.youtube.com/channel/UCP5tjEmvPItGyLhmjdwP7Ww)
- [Wendover Productions analysis](https://www.ad-hoc-news.de/boerse/news/ueberblick/wendover-productions-mania-the-smartest-content-machine-on-youtube-right/68489935)
- [Sleepy Time History channel](https://www.youtube.com/channel/UC6uGYezl7-dtRlaXohwo5ew)
- [ASMR YouTube Monetization 2026](https://asmrvideos.io/posts/asmr-youtube-monetization-guide-2026)
- [Oceanliner Designs](https://www.youtube.com/@OceanlinerDesigns/videos)
- [Brick Immortar](https://www.youtube.com/@BrickImmortar)
- [SCOTUS Oral Argument Transcripts](https://www.youtube.com/@SCOTUSOralArgument)
- [Library of Congress Chronicling America](https://chroniclingamerica.loc.gov/)
- [NASA Image and Video Library](https://images.nasa.gov/)
- [SEC EDGAR](https://www.sec.gov/edgar)
- [RECAP / CourtListener](https://www.courtlistener.com/recap/)
- [Chubbyemu earnings — networthspot](https://www.networthspot.com/chubbyemu/net-worth/)
