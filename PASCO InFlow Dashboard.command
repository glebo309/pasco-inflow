#!/bin/zsh
set -u

SCRIPT_DIR=${0:A:h}
VENV_DIR="$SCRIPT_DIR/.pasco-dashboard-venv"
REQUIREMENTS_FILE="$SCRIPT_DIR/requirements-dashboard.txt"
REQUIREMENTS_MARKER="$VENV_DIR/.requirements-sha256"
LAUNCH_LOG="$SCRIPT_DIR/launch.log"

cd "$SCRIPT_DIR" || exit 1

HEALTH="$(/usr/bin/curl -fsS http://localhost:9124/health 2>/dev/null)"
if [[ "$HEALTH" == "PASCO InFlow Dash @ $SCRIPT_DIR" ]]; then
  /usr/bin/open http://localhost:9124
  exit 0
elif [[ -n "$HEALTH" ]] || /usr/sbin/lsof -ti :9124 >/dev/null 2>&1; then
  # A dashboard from an older copy (or something else) still holds the port:
  # replace it so this folder's version is the one that runs.
  echo "Stopping an older PASCO InFlow that was still running..."
  /usr/sbin/lsof -ti :9124 | /usr/bin/xargs /bin/kill 2>/dev/null
  /bin/sleep 1
fi

# Zips arriving by AirDrop/mail are quarantined; clear it once so the bundled
# Python can run without Gatekeeper stopping every file.
/usr/bin/xattr -dr com.apple.quarantine "$SCRIPT_DIR" >/dev/null 2>&1

# Fully self-contained: this folder ships its own Python, so nothing needs to
# be downloaded or installed system-wide.
PYTHON_BIN=""
if [[ -x "$SCRIPT_DIR/python/bin/python3" ]]; then
  PYTHON_BIN="$SCRIPT_DIR/python/bin/python3"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /usr/bin/python3
  do
    if [[ -x "$candidate" ]]; then
      PYTHON_BIN="$candidate"
      break
    fi
  done

  # A fresh Mac ships /usr/bin/python3 only as a stub that needs Apple's free
  # command line tools. Detect that case and walk the user through it once.
  if [[ "$PYTHON_BIN" == "/usr/bin/python3" ]] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
    /usr/bin/osascript -e 'display dialog "PASCO InFlow needs Apple'\''s free command line tools (a one-time install of a few minutes).\n\nClick OK, then click Install in the window that appears. When the install finishes, double-click PASCO InFlow Dashboard again." buttons {"OK"} default button "OK"'
    /usr/bin/xcode-select --install >/dev/null 2>&1
    exit 0
  fi
fi

if [[ -z "$PYTHON_BIN" ]]; then
  /usr/bin/osascript -e 'display dialog "Python 3 is required to run PASCO InFlow." buttons {"OK"} default button "OK" with icon stop'
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo ""
  echo "  [1/3] First launch: preparing the private PASCO InFlow environment..."
  "$PYTHON_BIN" -m venv "$VENV_DIR" || {
    /usr/bin/osascript -e 'display dialog "PASCO InFlow could not create its Python environment. See launch.log next to the launcher." buttons {"OK"} default button "OK" with icon stop'
    exit 1
  }
fi

CURRENT_REQUIREMENTS="$(/usr/bin/shasum -a 256 "$REQUIREMENTS_FILE" | /usr/bin/awk '{print $1}')"
INSTALLED_REQUIREMENTS=""
if [[ -f "$REQUIREMENTS_MARKER" ]]; then
  INSTALLED_REQUIREMENTS="$(<"$REQUIREMENTS_MARKER")"
fi

WHEEL_DIR="$SCRIPT_DIR/wheels"

if [[ "$CURRENT_REQUIREMENTS" != "$INSTALLED_REQUIREMENTS" ]] || ! "$VENV_DIR/bin/python" -c 'import dash, numpy, pandas, plotly' >/dev/null 2>&1; then
  INSTALLED=""
  if [[ -d "$WHEEL_DIR" ]]; then
    echo ""
    echo "  [2/3] Installing packages from this folder (about half a minute)..."
    echo ""
    if "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --no-index --find-links "$WHEEL_DIR" --progress-bar on -r "$REQUIREMENTS_FILE"; then
      INSTALLED="yes"
    fi
  fi
  if [[ -z "$INSTALLED" ]]; then
    echo ""
    echo "  [2/3] Downloading packages (one time, needs internet) - progress below:"
    echo ""
    "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -q --upgrade pip wheel >>"$LAUNCH_LOG" 2>&1
    "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --progress-bar on -r "$REQUIREMENTS_FILE" || {
      /usr/bin/osascript -e 'display dialog "PASCO InFlow could not install its packages. Check the internet connection and double-click the launcher again." buttons {"OK"} default button "OK" with icon stop'
      exit 1
    }
  fi
  print -r -- "$CURRENT_REQUIREMENTS" > "$REQUIREMENTS_MARKER"
  echo ""
  echo "  Packages installed."
fi

echo ""
echo "  [3/3] PASCO InFlow is starting - the browser will open by itself."
echo "  Keep THIS window open while measuring. Closing it stops the dashboard."
echo ""

"$VENV_DIR/bin/python" "$SCRIPT_DIR/pasco_dash_app.py" >>"$LAUNCH_LOG" 2>&1 &
SERVER_PID=$!

STARTED=""
for attempt in {1..60}; do
  if [[ "$(/usr/bin/curl -fsS http://localhost:9124/health 2>/dev/null)" == "PASCO InFlow Dash"* ]]; then
    /usr/bin/open http://localhost:9124
    STARTED="yes"
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    break
  fi
  /bin/sleep 0.25
done

if [[ -z "$STARTED" ]]; then
  /usr/bin/osascript -e 'display dialog "PASCO InFlow could not start. Send launch.log (next to the launcher) to Glenn." buttons {"OK"} default button "OK" with icon stop'
  exit 1
fi

wait "$SERVER_PID"

echo
read "?PASCO InFlow stopped. Press Return to close."
