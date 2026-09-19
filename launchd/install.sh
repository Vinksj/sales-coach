#!/bin/sh
# Install (or reinstall) the always-on sales coach server as a user launch agent (macOS).
# It starts at login, restarts if it dies, and logs to ~/Library/Logs/salescoach/serve.log.
#
#   sh launchd/install.sh                       install for this checkout and this user
#   sh launchd/install.sh --generate-only FILE  write the plist to FILE and stop: nothing is
#                                               installed, launchctl is never called
#
# The plist is generated from salescoach.plist.template at install time, from:
#   * where this checkout is (the folder above launchd/),
#   * the current user's HOME,
#   * a PATH made of the folders where `claude`, `ffmpeg` and `ollama` are found (each optional).
# Optional environment:
#   SALESCOACH_BIN    the salescoach executable (default: <checkout>/.venv/bin/salescoach,
#                     else the one on PATH)
#   SALESCOACH_PORT   default 8140
#   SALESCOACH_CONFIG, SALESCOACH_DATA, SALESCOACH_SETTINGS, SALESCOACH_RUNTIME, SALES_DB
#                     when set now, they are written into the agent so the server uses the same
#                     folders as your shell does
set -eu

LABEL="com.salescoach.serve"
LEGACY_LABEL="com.ordinal.salescoach"      # the label this project used before it was generalised

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TEMPLATE="$HERE/salescoach.plist.template"
PORT="${SALESCOACH_PORT:-8140}"
LOG_DIR="$HOME/Library/Logs/salescoach"
LOG="$LOG_DIR/serve.log"
AGENTS_DIR="$HOME/Library/LaunchAgents"

GENERATE_ONLY=""
if [ "${1:-}" = "--generate-only" ]; then
    GENERATE_ONLY="${2:-}"
    if [ -z "$GENERATE_ONLY" ]; then
        echo "usage: sh launchd/install.sh --generate-only FILE" >&2
        exit 2
    fi
elif [ "$#" -gt 0 ]; then
    echo "usage: sh launchd/install.sh [--generate-only FILE]" >&2
    exit 2
fi

case "$PORT" in
    ''|*[!0-9]*) echo "SALESCOACH_PORT must be a number, got '$PORT'" >&2; exit 2 ;;
esac
[ -f "$TEMPLATE" ] || { echo "missing $TEMPLATE" >&2; exit 1; }

# ---- the salescoach executable ---------------------------------------------------------------
BIN="${SALESCOACH_BIN:-}"
if [ -z "$BIN" ]; then
    if [ -x "$ROOT/.venv/bin/salescoach" ]; then
        BIN="$ROOT/.venv/bin/salescoach"
    else
        BIN="$(command -v salescoach 2>/dev/null || true)"
    fi
fi
if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
    echo "salescoach is not installed. From $ROOT run:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -e ." >&2
    echo "or point SALESCOACH_BIN at the executable." >&2
    exit 1
fi

# ---- PATH: only what is really on this machine -----------------------------------------------
# launchd starts agents with a bare PATH, and the server shells out to claude (the Claude CLI
# provider, the calendar, Granola), ffmpeg (audio import) and ollama. Each is optional.
TOOL_PATH=""
FOUND=""
add_dir() {
    case ":$TOOL_PATH:" in
        *":$1:"*) ;;
        *) TOOL_PATH="${TOOL_PATH:+$TOOL_PATH:}$1" ;;
    esac
}
for tool in claude ffmpeg ollama; do
    where="$(command -v "$tool" 2>/dev/null || true)"
    case "$where" in
        /*) ;;                                  # a real file, not an alias or a shell function
        *) where="" ;;
    esac
    if [ -z "$where" ] && [ "$tool" = "claude" ] && [ -x "$HOME/.local/bin/claude" ]; then
        where="$HOME/.local/bin/claude"         # where the Claude CLI installs itself
    fi
    if [ -n "$where" ]; then
        add_dir "$(dirname "$where")"
        FOUND="${FOUND:+$FOUND, }$tool"
    else
        echo "note: $tool was not found; the features that need it stay off until it is installed and this script is run again" >&2
    fi
done
for dir in /usr/local/bin /usr/bin /bin /usr/sbin /sbin; do
    add_dir "$dir"
done

# ---- fill the template -----------------------------------------------------------------------------
# A value is XML-escaped first, then escaped for the right-hand side of sed's s|||.
esc() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/[\\&|]/\\&/g'
}
refuse_newline() {
    case "$1" in
        *"
"*) echo "a path or value contains a line break; refusing to write a plist from it" >&2; exit 1 ;;
    esac
}

EXTRA_ENV=""
for name in SALESCOACH_CONFIG SALESCOACH_DATA SALESCOACH_SETTINGS SALESCOACH_RUNTIME SALES_DB; do
    eval "value=\${$name:-}"
    if [ -n "$value" ]; then
        refuse_newline "$value"
        EXTRA_ENV="$EXTRA_ENV    <key>$name</key><string>$(esc "$value")</string>"
    fi
done

for value in "$BIN" "$ROOT" "$HOME" "$TOOL_PATH" "$LOG"; do
    refuse_newline "$value"
done

generate() {
    sed -e "s|@@LABEL@@|$(esc "$LABEL")|g" \
        -e "s|@@SALESCOACH_BIN@@|$(esc "$BIN")|g" \
        -e "s|@@PORT@@|$PORT|g" \
        -e "s|@@ROOT@@|$(esc "$ROOT")|g" \
        -e "s|@@PATH@@|$(esc "$TOOL_PATH")|g" \
        -e "s|@@HOME@@|$(esc "$HOME")|g" \
        -e "s|@@LOG@@|$(esc "$LOG")|g" \
        -e "s|@@EXTRA_ENV@@|$EXTRA_ENV|g" \
        "$TEMPLATE"
}

if [ -n "$GENERATE_ONLY" ]; then
    generate > "$GENERATE_ONLY"
    echo "wrote $GENERATE_ONLY (nothing installed)"
    exit 0
fi

# ---- install ------------------------------------------------------------------------------------------
if [ "$(uname -s)" != "Darwin" ]; then
    echo "launchd is macOS only. On other systems run 'salescoach serve' under your own service manager." >&2
    exit 1
fi

DOMAIN="gui/$(id -u)"
PLIST="$AGENTS_DIR/$LABEL.plist"
mkdir -p "$AGENTS_DIR" "$LOG_DIR"

wait_gone() {
    # bootout returns before the job is gone; bootstrapping too early fails with an I/O error.
    n=0
    while launchctl print "$DOMAIN/$1" >/dev/null 2>&1 && [ "$n" -lt 20 ]; do
        sleep 0.5
        n=$((n + 1))
    done
}

# The legacy agent first, so two servers never fight over the same port. Its plist is removed as
# well: left in place, launchd would load it again at the next login.
if launchctl print "$DOMAIN/$LEGACY_LABEL" >/dev/null 2>&1; then
    echo "stopping the legacy agent $LEGACY_LABEL"
    launchctl bootout "$DOMAIN/$LEGACY_LABEL" 2>/dev/null || true
    wait_gone "$LEGACY_LABEL"
fi
rm -f "$AGENTS_DIR/$LEGACY_LABEL.plist"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
wait_gone "$LABEL"

TMP="$(mktemp "$AGENTS_DIR/.$LABEL.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
generate > "$TMP"
plutil -lint "$TMP" >/dev/null
chmod 644 "$TMP"
mv "$TMP" "$PLIST"
trap - EXIT

launchctl bootstrap "$DOMAIN" "$PLIST"
sleep 3
launchctl print "$DOMAIN/$LABEL" | grep -E "state = |pid = " || true
echo "installed: $LABEL -> http://127.0.0.1:$PORT (log: $LOG)"
echo "  program: $BIN"
echo "  tools on its PATH: ${FOUND:-none found}"
