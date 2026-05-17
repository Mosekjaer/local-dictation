#!/usr/bin/env bash
# local-dictation installer for Pop!_OS COSMIC.
#
# Usage: ./install.sh [--with-applet]
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
#   9. If --with-applet: cargo install the COSMIC panel applet binaries
#      and print panel-config edit instructions.

set -euo pipefail

WITH_APPLET=0
for arg in "$@"; do
    case "${arg}" in
        --with-applet) WITH_APPLET=1 ;;
        -h|--help)
            cat <<'EOF'
Usage: ./install.sh [--with-applet]

  --with-applet  Also build and install the COSMIC panel applet binaries
                 (local-dictation-applet-left, local-dictation-applet-right).
                 Requires cargo. Cold build takes ~2 minutes.
EOF
            exit 0
            ;;
        *) echo "install.sh: unknown argument: ${arg}" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${HOME}/.local/share/local-dictation/.venv"
BIN_LINK="${HOME}/.local/bin/local-dictation"
CONFIG_DIR="${HOME}/.config/local-dictation"
CONFIG_FILE="${CONFIG_DIR}/config.toml"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
APPLET_WORKSPACE="${SCRIPT_DIR}/cosmic-applet"
APPLET_DESKTOP_DIR="${HOME}/.local/share/applications"
PANEL_CONFIG_DIR="${HOME}/.config/cosmic/com.system76.CosmicPanel.Panel/v1"

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

# 9. Optional: COSMIC panel applet.
if [[ "${WITH_APPLET}" -eq 1 ]]; then
    if ! command -v cargo >/dev/null 2>&1; then
        fatal "cargo not found on PATH (needed for --with-applet)"
    fi
    if [[ ! -d "${APPLET_WORKSPACE}" ]]; then
        fatal "applet workspace not found at ${APPLET_WORKSPACE}"
    fi
    info "building COSMIC panel applet binaries (cold build can take ~2 minutes)"
    cargo install \
        --path "${APPLET_WORKSPACE}/local-dictation-applet-left" \
        --root "${HOME}/.local" \
        --quiet
    cargo install \
        --path "${APPLET_WORKSPACE}/local-dictation-applet-right" \
        --root "${HOME}/.local" \
        --quiet
    info "installed local-dictation-applet-{left,right} into ${HOME}/.local/bin"

    install -D -m 0644 \
        "${APPLET_WORKSPACE}/resources/local-dictation-applet-left.desktop" \
        "${APPLET_DESKTOP_DIR}/local-dictation-applet-left.desktop"
    install -D -m 0644 \
        "${APPLET_WORKSPACE}/resources/local-dictation-applet-right.desktop" \
        "${APPLET_DESKTOP_DIR}/local-dictation-applet-right.desktop"
    info "installed .desktop files into ${APPLET_DESKTOP_DIR}"
fi

# 10. Next steps.
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

if [[ "${WITH_APPLET}" -eq 1 ]]; then
    cat <<EOF
COSMIC panel applet — manual config edit required
-------------------------------------------------
The two applet binaries are installed but COSMIC won't show them until
their IDs appear in the panel config. Edit:

    ${PANEL_CONFIG_DIR}/plugins_center

To insert the two IDs around CosmicAppletTime, the file should contain:

    Some([
        "local-dictation-applet-left",
        "com.system76.CosmicAppletTime",
        "local-dictation-applet-right",
    ])

After saving, restart the panel so it picks up the change:

    systemctl --user restart cosmic-panel.service
    # or, if that unit doesn't exist on your system:
    pkill -USR2 cosmic-panel || pkill cosmic-panel

If you previously had "super-stt-cosmic-applet-{left,right}" in
plugins_center, replace those entries with the new IDs above.

EOF
fi
