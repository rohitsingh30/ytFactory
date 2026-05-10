# yt-dlp `--download-sections` overwrites instead of concatenating

**Surfaced:** 2026-05-08, /clone-video-format on Mega Football Hearts
video (https://youtu.be/BQkYsINy95k).

**Symptom:** A single yt-dlp invocation with three
`--download-sections` flags (intended for hook + mid + closer
windows) produced a 60-second file containing only the first window.
No error, no warning — just a shorter mp4.

**Root cause:** yt-dlp's argparse maps `--download-sections` to a
single string, not a list. Multiple flags overwrite each other; only
the LAST one applies.

**Classification:** CLASS-OF-BUG. Will hit any future
/clone-video-format run that tries the windowed approach for >15 min
sources.

**Fix shipped 2026-05-08:** SKILL.md step 2 updated to default to
whole-file 360p download for sources ≤15 min. For sources >15 min,
either fetch whole or run yt-dlp three separate times with one
`--download-sections` each, then concat with ffmpeg.

**Memory mirror:**
[`feedback_yt_dlp_section_overwrite.md`](/Users/rohit/.claude/projects/-Users-rohit-ytFactory/memory/feedback_yt_dlp_section_overwrite.md)
