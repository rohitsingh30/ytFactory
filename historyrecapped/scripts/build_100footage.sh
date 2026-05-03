#!/bin/bash
# Build a 100%-footage historyrecapped short — no image-gen, just archival cuts under our narrator.
set -euo pipefail

SRC=sportstoriesanimated/footage/sources/agDllH81pb0.mp4
NARR=historyrecapped/cache/battle-of-britain-few/narration.wav
WORK=historyrecapped/scratch
OUT=historyrecapped/shorts/battle-of-britain-few-100footage.mp4

mkdir -p "$WORK"

# 7 windows totalling ~50s, mapped to narration arc:
# 0-7s   : pilots silhouettes / tarmac at dawn        (intro: "Summer 1940. France has fallen.")
# 7-14s  : bombers crossing in formation              ("Luftwaffe must destroy the RAF")
# 14-22s : RAF pilot prep + cockpit                   ("front line. Pilots scramble.")
# 22-30s : Spitfire engagement / cockpit POV          ("decisive day. RAF puts up every fighter.")
# 30-38s : aerial dogfight gun-camera                 ("German bomber losses catastrophic")
# 38-44s : bomber shot down + smoke                   ("Hitler postpones invasion")
# 44-50s : Spitfire flying calm                       (Churchill quote + closer)
WINDOWS=(
  "240.0 7.0"
  "1296.0 7.0"
  "273.0 8.0"
  "1100.0 8.0"
  "1410.0 8.0"
  "1418.0 6.0"
  "1530.0 13.0"
)

LETTERBOX="[0:v]split=2[bg][fg];[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,gblur=sigma=24,eq=brightness=-0.15[bg_blur];[fg]scale=1080:-2[fg_scaled];[bg_blur][fg_scaled]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]"

# Trim each window with blurred-letterbox + uniform 30fps + mute audio
i=0
for w in "${WINDOWS[@]}"; do
  ss=${w%% *}
  t=${w##* }
  out="$WORK/clip_$i.mp4"
  echo "[trim] window $i: ss=$ss t=$t -> $out"
  ffmpeg -y -hide_banner -loglevel error \
    -ss "$ss" -t "$t" -i "$SRC" \
    -filter_complex "$LETTERBOX" \
    -map "[vout]" \
    -an \
    -c:v libx264 -pix_fmt yuv420p -r 30 \
    "$out"
  i=$((i+1))
done

# Concat list
LIST="$WORK/concat.txt"
> "$LIST"
for ((j=0; j<i; j++)); do
  echo "file 'clip_$j.mp4'" >> "$LIST"
done
cat "$LIST"

# Concat the trimmed clips into one silent video
echo "[concat] -> $WORK/video.mp4"
ffmpeg -y -hide_banner -loglevel error \
  -f concat -safe 0 -i "$LIST" \
  -c:v libx264 -pix_fmt yuv420p -r 30 \
  -an \
  -movflags +faststart \
  "$WORK/video.mp4"

# Probe video duration vs narration
VID_DUR=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$WORK/video.mp4")
NARR_DUR=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$NARR")
echo "[durations] video=$VID_DUR  narration=$NARR_DUR"

# Mux narration onto the silent video; if narration is slightly longer, video freezes on last frame for the tail
echo "[mux] -> $OUT"
mkdir -p "$(dirname "$OUT")"
ffmpeg -y -hide_banner -loglevel error \
  -i "$WORK/video.mp4" \
  -i "$NARR" \
  -filter_complex "[0:v]tpad=stop_mode=clone:stop_duration=2[vpad]" \
  -map "[vpad]" -map 1:a \
  -c:v libx264 -pix_fmt yuv420p -r 30 \
  -c:a aac -b:a 192k \
  -shortest \
  -movflags +faststart \
  "$OUT"

# Final probe
ls -lh "$OUT"
ffprobe -v error -show_entries format=duration:stream=width,height,codec_name -of default "$OUT"
