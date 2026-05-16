#!/usr/bin/env bash
# local-dictation installer for Pop!_OS COSMIC.
#
# Steps:
#   1. Verify Python 3.10+.
#   2. Create a dedicated venv at ~/.local/share/local-dictation/.venv.
#   3. Install this project into it.
#   4. Symlink the entry point into ~/.local/bin.
#   5. Copy config.example.toml into ~/.config/local-dictation/config.toml
#      (only if absent).
#   6. Install systemd --user units for local-dictation and ydotoold.
#   7. Reload systemd and enable both units.
#   8. Print follow-up steps for the COSMIC keyboard shortcut.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${HOME}/.local/share/local-dictation/.venv"
BIN_LINK="${HOME}/.local/bin/local-dictation"
CONFIG_DIR="${HOME}/.config/local-dictation"
CONFIG_FILE="${CONFIG_DIR}/config.toml"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

info()  { printf '[install] %s\n' "$*"; }
warn()  { printf '[install] WARN: %s\n' "$*" >&2; }
fatal() { printf '[install] FATAL: %s\n' "$*" >&2; exit 1; }

# 1. Python version check.
if ! command -v python3 >/dev/null 2>&1; then
    fatal "python3 not found on PATH"
fi
PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3,10) else 0)')"
if [[ "${PY_OK}" != "1" ]]; then
    fatal "python3 ${PY_VERSION} is too old; need >= 3.10"
fi
info "python3 ${PY_VERSION} OK"

# 2. Venv.
info "creating venv at ${VENV_DIR}"
mkdir -p "$(dirname "${VENV_DIR}")"
python3 -m venv "${VENV_DIR}"

# Upgrade pip inside the venv. Quiet but not silent.
"${VENV_DIR}/bin/python" -m pip install --upgrade --quiet pip

# 3. Install project from repo root.
info "installing local-dictation from ${SCRIPT_DIR}"
"${VENV_DIR}/bin/pip" install --quiet "${SCRIPT_DIR}"

# 4. Symlink CLI entry point.
mkdir -p "$(dirname "${BIN_LINK}")"
ln -sf "${VENV_DIR}/bin/local-dictation" "${BIN_LINK}"
info "symlinked ${BIN_LINK} -> ${VENV_DIR}/bin/local-dictation"

# 5. Default config.
if [[ -e "${CONFIG_FILE}" ]]; then
    info "config already exists at ${CONFIG_FILE} (leaving untouched)"
else
    install -D -m 0644 "${SCRIPT_DIR}/config.example.toml" "${CONFIG_FILE}"
    info "wrote default config to ${CONFIG_FILE}"
fi

# 6. systemd user units.
install -D -m 0644 \
    "${SCRIPT_DIR}/systemd/local-dictation.service" \
    "${SYSTEMD_USER_DIR}/local-dictation.service"
install -D -m 0644 \
    "${SCRIPT_DIR}/systemd/ydotoold.service" \
    "${SYSTEMD_USER_DIR}/ydotoold.service"
info "installed systemd --user units"

# 7. Reload and enable.
if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not found; skipping enable/start"
else
    systemctl --user daemon-reload
    systemctl --user enable --now ydotoold.service || \
        warn "could not start ydotoold.service (is ydotoold installed?)"
    systemctl --user enable --now local-dictation.service || \
        warn "could not start local-dictation.service (run 'local-dictation doctor' to diagnose)"
fi

# 8. Next steps.
cat <<'EOF'

[install] done.

Next steps
----------
1. Run the doctor to verify your environment:
       local-dictation doctor

2. Bind the push-to-talk shortcut in COSMIC:
   Settings -> Input -> Keyboard -> Keyboard Shortcuts -> Custom Shortcuts
       Command:       local-dictation toggle
       Key combo:     Super+Space

   IMPORTANT: COSMIC ships Super+Space as "switch input source" by
   default. You must unbind that default first, or your custom shortcut
   will never fire.

3. Tail the daemon log if you hit problems:
       journalctl --user -u local-dictation -f
       tail -f ~/.cache/local-dictation/local-dictation.log

EOF
