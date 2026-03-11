# Grok Video Studio

set dotenv-load

venv := ".venv"
python := venv / "bin/python"
pip := venv / "bin/pip"
uvicorn := venv / "bin/uvicorn"

# List available commands
default:
    @just --list

# Verify all prerequisites are installed
check:
    #!/usr/bin/env bash
    set -euo pipefail
    ok=true

    check() {
        if command -v "$1" &>/dev/null; then
            printf "  %-12s %s\n" "$1" "$(eval "$2" 2>&1 | head -1)"
        else
            printf "  %-12s MISSING\n" "$1"
            ok=false
        fi
    }

    echo "Prerequisites:"
    check python3   "python3 --version"
    check node      "node --version"
    check npm       "npm --version"
    check ffmpeg    "ffmpeg -version"
    check ffprobe   "ffprobe -version"

    if [ -f "{{ venv }}/bin/python" ]; then
        printf "  %-12s %s\n" "venv" "ok ({{ venv }})"
    else
        printf "  %-12s MISSING — run 'just setup'\n" "venv"
        ok=false
    fi

    if [ -d "web/node_modules" ]; then
        printf "  %-12s %s\n" "node_modules" "ok"
    else
        printf "  %-12s MISSING — run 'just setup'\n" "node_modules"
        ok=false
    fi

    echo ""
    if $ok; then
        echo "All good."
    else
        echo "Some prerequisites are missing. Install them and run 'just setup'."
        exit 1
    fi

# Create venv and install all dependencies
setup:
    python3 -m venv {{ venv }}
    {{ pip }} install -r requirements.txt
    cd web && npm install

# Install Python dependencies only
setup-api:
    python3 -m venv {{ venv }}
    {{ pip }} install -r requirements.txt

# Install Node dependencies only
setup-web:
    cd web && npm install

# Start the API server (port 8000)
api:
    {{ uvicorn }} api.main:app --reload --port 8000

# Start the frontend dev server (port 5173)
web:
    cd web && npm run dev

# Start both servers in parallel
dev:
    #!/usr/bin/env bash
    trap 'kill 0' EXIT
    {{ uvicorn }} api.main:app --reload --port 8000 &
    cd web && npm run dev &
    wait

stop-web:
    #!/usr/bin/env bash
    set -euo pipefail
    root="$(pwd)"
    stop_pid() {
        pid="$1"
        kill -INT "$pid" 2>/dev/null || true
        sleep 0.5
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            sleep 0.5
        fi
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    }
    for port in 5173 5174 5175 5176 5177; do
        pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u || true)"
        if [ -n "$pids" ]; then
            for pid in $pids; do
                cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
                if echo "$cmd" | grep -q "$root/web"; then
                    stop_pid "$pid"
                fi
            done
        fi
    done
    pids="$(pgrep -f "$root/web/node_modules/.bin/vite" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        for pid in $pids; do
            stop_pid "$pid"
        done
    fi

stop-api:
    #!/usr/bin/env bash
    set -euo pipefail
    root="$(pwd)"
    stop_pid() {
        pid="$1"
        kill -INT "$pid" 2>/dev/null || true
        sleep 0.5
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            sleep 0.5
        fi
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    }
    pids="$(lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null | sort -u || true)"
    if [ -n "$pids" ]; then
        for pid in $pids; do
            cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
            if echo "$cmd" | grep -q "$root"; then
                stop_pid "$pid"
            fi
        done
    fi
    pids="$(pgrep -f "$root/.venv/bin/python3 .*api.main:app" 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        for pid in $pids; do
            stop_pid "$pid"
        done
    fi

stop: stop-web stop-api

# Build the frontend for production
build:
    cd web && npm run build

# Clean generated sessions, logs, and build artifacts
clean:
    rm -rf sessions/
    rm -f generate_video.log generate_multi_video.log
    cd web && rm -rf dist

# Clean everything including venv and node_modules
clean-all: clean
    rm -rf {{ venv }}
    cd web && rm -rf node_modules

# Verify sessions and report missing files (dry run)
verify-sessions:
    {{ python }} verify_sessions.py --dry-run

# Verify sessions and download missing files
fix-sessions:
    {{ python }} verify_sessions.py

# Show current version
version:
    @cat VERSION
