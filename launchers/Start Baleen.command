#!/bin/sh
# Start Baleen (macOS). Double-click this file: your browser opens Baleen, and the Terminal
# window shows the log. Closing the window stops Baleen. Spec section 14.3.

here=$(cd "$(dirname "$0")" && pwd -P) || { echo "Cannot open the Baleen folder."; exit 3; }
BALEEN_HOME=$here
unset PYTHONHOME PYTHONPATH

# Source checkout (launchers/ inside the repository): use the repository root and its src/.
checkout=
if [ -f "$here/../pyproject.toml" ] && [ -f "$here/../src/baleen/__main__.py" ]; then
    checkout=1
    BALEEN_HOME=$(cd "$here/.." && pwd -P)
    PYTHONPATH="$BALEEN_HOME/src"
    export PYTHONPATH
fi
cd "$BALEEN_HOME" || exit 3
export BALEEN_HOME

# All state stays inside the folder: data/ holds settings, logs, temporary files and Java prefs.
data="$BALEEN_HOME/data"
mkdir -p "$data/tmp" "$data/java"
export TMPDIR="$data/tmp"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export JAVA_TOOL_OPTIONS="-Djava.util.prefs.userRoot=\"$data/java\" -Djava.util.prefs.systemRoot=\"$data/java\" -Djava.io.tmpdir=\"$data/tmp\" -XX:-UsePerfData"

# Bundled Python first; a source checkout without runtime/ falls back to Python 3.12 on PATH.
py="$BALEEN_HOME/runtime/python/bin/python3"
if [ ! -x "$py" ]; then
    echo "runtime/ not found: development mode, using Python from PATH (3.12 required)."
    py=$(command -v python3.12 || command -v python3 || true)
    if [ -z "$py" ]; then
        echo "runtime/ is missing and no Python 3.12 was found on PATH."
        echo "Use the complete Baleen folder from the release zip."
        printf "Press Return to close this window. "
        read -r _
        exit 3
    fi
elif xattr -p com.apple.quarantine "$py" >/dev/null 2>&1; then
    echo "WARNING: this folder is still quarantined by macOS, so the bundled programs may be"
    echo "blocked. Quit Baleen, then run this once in Terminal and start Baleen again:"
    echo "  xattr -dr com.apple.quarantine \"$BALEEN_HOME\""
    echo
fi
[ -n "$checkout" ] && echo "Source checkout: using $BALEEN_HOME/src"

echo "Starting Baleen from \"$BALEEN_HOME\""
echo "Close this window to stop Baleen."
echo
"$py" -m baleen serve
rc=$?
if [ "$rc" -ne 0 ]; then
    echo
    echo "Baleen stopped with exit code $rc. The messages above say why."
    printf "Press Return to close this window. "
    read -r _
fi
exit "$rc"
