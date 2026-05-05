#!/usr/bin/env bash
# Set up YouTube cookies for yt-dlp so footage downloads (rivalry recap,
# sports stories, cosmos decoder, archival history) survive YouTube's
# bot-detection block. One-time setup — see
# memory/feedback_footage_yt_dlp_canonical for the principle.

set -e

DEFAULT_PATH="$HOME/.config/ytdlp/youtube_cookies.txt"
TARGET="${YTFACTORY_YTDLP_COOKIES:-$DEFAULT_PATH}"

echo "ytFactory yt-dlp cookie setup"
echo "============================="
echo ""

if [[ -f "$TARGET" ]]; then
    AGE_DAYS=$(( ( $(date +%s) - $(stat -f %m "$TARGET" 2>/dev/null || stat -c %Y "$TARGET") ) / 86400 ))
    echo "✓ Cookies file found at: $TARGET"
    echo "  Age: $AGE_DAYS days"
    if (( AGE_DAYS > 30 )); then
        echo ""
        echo "⚠ Cookies are >30 days old — YouTube may have rotated them."
        echo "  Re-export by following the steps below if downloads start failing."
    fi
    echo ""
    if [[ -z "${YTFACTORY_YTDLP_COOKIES:-}" ]]; then
        echo "Add this to your ~/.zshrc (or ~/.bashrc):"
        echo ""
        echo "  export YTFACTORY_YTDLP_COOKIES=\"$TARGET\""
        echo ""
        echo "Then: source ~/.zshrc"
    else
        echo "✓ YTFACTORY_YTDLP_COOKIES is exported."
        echo ""
        echo "Smoke test:"
        echo "  .venv/bin/python -m yt_dlp --cookies \"\$YTFACTORY_YTDLP_COOKIES\" \\"
        echo "      -f 'best[ext=mp4]' --skip-download \\"
        echo "      'https://www.youtube.com/watch?v=dQw4w9WgXcQ'"
    fi
    exit 0
fi

cat <<'EOF'
✗ No cookies file found.

One-time setup (~3 minutes):

1. Install a Chrome extension that exports cookies in Netscape format:
   https://chromewebstore.google.com/search/get%20cookies.txt

   Recommended: "Get cookies.txt LOCALLY"
   (no permissions, runs entirely client-side).

2. Open https://www.youtube.com in Chrome while logged in.

3. Click the extension icon → "Export" → save as:
EOF

echo "      $TARGET"

cat <<'EOF'

   (Create the directory first if needed: mkdir -p ~/.config/ytdlp)

4. Add to your ~/.zshrc (or ~/.bashrc):
EOF

echo "      export YTFACTORY_YTDLP_COOKIES=\"$TARGET\""

cat <<'EOF'

5. Reload your shell:
      source ~/.zshrc

6. Retry the render — yt-dlp will now authenticate and downloads will
   pass YouTube's bot check.

Cookies expire eventually (YouTube rotates session tokens). If a render
fails with "Sign in to confirm you're not a bot" months from now, just
re-export from the extension.
EOF

exit 1
