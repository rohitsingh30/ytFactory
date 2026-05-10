---
name: Sports footage uses blurred-letterbox, NOT center-crop, for 16:9 → 9:16
description: 16:9 broadcast → 9:16 portrait must use the Instagram/TikTok blurred-letterbox convention so ball + players stay fully visible; center-crop chops the sides where the action lives
type: feedback
originSessionId: 6d719008-9aa3-4424-b0ab-164cfc9e91a1
---
When `pipeline/footage.py` converts 16:9 broadcast clips to 1080×1920 portrait, the conversion MUST be a **blurred-letterbox composition** — not a `force_original_aspect_ratio=increase + crop` center-crop. The center-crop approach upscales ~1.78x vertically (destroys broadcast quality) and throws away the left and right thirds of the frame — which is exactly where the passer, the receiving runner, and the ball trajectory usually live in a sports broadcast wide shot.

**Why:** caught on v9 Aguero 93:20 render — the live broadcast showed Balotelli sliding the pass from the left edge and Aguero running on from the right; center-crop hid both. User flagged: "the football should be visible in the footage where it is being passed and when shot."

**How to apply:**
- The ffmpeg filter for footage clips is now a `filter_complex` with this shape (see `pipeline/footage.py:fetch_clip`):
  ```
  [0:v]split=2[bg][fg];
  [bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=24,eq=brightness=-0.15[bg_blur];
  [fg]scale=1080:-2[fg_scaled];
  [bg_blur][fg_scaled]overlay=(W-w)/2:(H-h)/2,fade=...[vout]
  ```
- Foreground clip is scaled to 1080 wide preserving aspect (1080×608 for 16:9 source). Centred vertically. Source-quality broadcast in the middle ~31% of the screen height.
- Background is the same clip cover-scaled + cropped + heavily blurred (sigma=24) and dimmed -15% so it doesn't compete with the foreground.
- Result: ball, passer, runner all visible. Source pixels never upscaled past 1x. Looks intentional (Instagram-style edit), not amateur (black bars).
- Don't override this with a different aspect mode in channel YAML or per-story config. The convention is set at the footage.py module level for ALL channels using `kind: footage` beats.