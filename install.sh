#!/usr/bin/env bash
# Install the ask-chatgpt and chatgpt-review skills into ~/.claude/skills.
#   ./install.sh          copy the skills
#   ./install.sh --link   symlink them to this checkout (follows `git pull`)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
dest="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
mode="copy"
[[ "${1:-}" == "--link" ]] && mode="link"

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
command -v peekaboo >/dev/null || echo "warning: peekaboo not found — brew install openclaw/tap/peekaboo" >&2

mkdir -p "$dest"
for skill in ask-chatgpt chatgpt-review; do
  target="$dest/$skill"
  if [[ -e "$target" || -L "$target" ]]; then
    backup="$target.bak-$(date +%Y%m%d-%H%M%S)"
    mv "$target" "$backup"
    echo "existing $skill moved to $backup"
  fi
  if [[ "$mode" == "link" ]]; then
    ln -s "$here/skills/$skill" "$target"
  else
    mkdir -p "$target"
    (cd "$here/skills/$skill" && find . -type f ! -name '._*' ! -name '.DS_Store' ! -path '*/__pycache__/*' \
      -exec sh -c 'mkdir -p "$0/$(dirname "$1")" && cp "$1" "$0/$1"' "$target" {} \;)
  fi
  echo "installed $skill -> $target ($mode)"
done

echo
echo "Next: start Peekaboo.app, grant it Screen Recording + Accessibility, open ChatGPT, then run"
echo "  python3 \"$dest/ask-chatgpt/chatgpt_bridge.py\" status"
