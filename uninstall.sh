#!/usr/bin/env bash
# local-dictation uninstaller.
#
# Default run removes the venv, CLI symlink, and systemd user units.
# --purge additionally removes ~/.config/local-dictation and
# ~/.cache/local-dictation.

set -euo pipefail

PURGE=0
for arg in "$@"; do
    case "${arg}" in
        --purge) PURGE=1 ;;
        -h|--help)
            cat <<'EOF'
Usage: ./uninstall.sh [--purge]

  --purge   Also delete ~/.config/local-dictation and ~/.cache/local-dictation.
EOF
            exit 0
            ;;
        *) echo "uninstall.sh: unknown argument: ${arg}" >&2; exit 2 ;;
    esac
done

BIN_LINK="${HOME}/.local/bin/local-dictation"
VENV_DIR="${HOME}/.local/share/local-dictation"
CONFIG_DIR="${HOME}/.config/local-dictation"
CACHE_DIR="${HOME}/.cache/local-dictation"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
APPLET_BIN_LEFT="${HOME}/.local/bin/local-dictation-applet-left"
APPLET_BIN_RIGHT="${HOME}/.local/bin/local-dictation-applet-right"
APPLET_DESKTOP_LEFT="${HOME}/.local/share/applications/local-dictation-applet-left.desktop"
APPLET_DESKTOP_RIGHT="${HOME}/.local/share/applications/local-dictation-applet-right.desktop"

info() { printf '[uninstall] %s\n' "$*"; }

# 1. Stop and disable units. Suppress errors if they were never installed.
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now local-dictation.service 2>/dev/null || true
    systemctl --user disable --now ydotoold.service        2>/dev/null || true
fi

# 2. Remove unit files.
rm -f "${SYSTEMD_USER_DIR}/local-dictation.service"
rm -f "${SYSTEMD_USER_DIR}/ydotoold.service"

# 3. Reload systemd.
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload 2>/dev/null || true
fi

# 4. Remove CLI symlink.
if [[ -L "${BIN_LINK}" || -e "${BIN_LINK}" ]]; then
    rm -f "${BIN_LINK}"
    info "removed ${BIN_LINK}"
fi

# 5. Remove venv.
if [[ -d "${VENV_DIR}" ]]; then
    rm -rf "${VENV_DIR}"
    info "removed ${VENV_DIR}"
fi

# 5b. Remove COSMIC applet binaries and .desktop entries (if present).
# Never touches ~/.config/cosmic/com.system76.CosmicPanel.Panel/v1/plugins_center —
# that is user data; the user removes the IDs themselves.
for f in "${APPLET_BIN_LEFT}" "${APPLET_BIN_RIGHT}" \
         "${APPLET_DESKTOP_LEFT}" "${APPLET_DESKTOP_RIGHT}"; do
    if [[ -e "${f}" ]]; then
        rm -f "${f}"
        info "removed ${f}"
    fi
done

# 6. Purge config + cache when asked.
if [[ "${PURGE}" -eq 1 ]]; then
    if [[ -d "${CONFIG_DIR}" ]]; then
        rm -rf "${CONFIG_DIR}"
        info "removed ${CONFIG_DIR}"
    fi
    if [[ -d "${CACHE_DIR}" ]]; then
        rm -rf "${CACHE_DIR}"
        info "removed ${CACHE_DIR}"
    fi
    info "removed config and cache"
else
    info "left config (${CONFIG_DIR}) and cache (${CACHE_DIR}) in place; pass --purge to remove"
fi

info "done"
