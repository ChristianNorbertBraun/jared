#!/bin/bash
# Run as an admin user:   <command that prints the token> | sudo -u jared bash ~jared/current/setup/set-paperless-token.sh
# Stores the API token of a VIEW-ONLY Paperless account (file paperless-token, mode 600), read from standard input so
# it never appears in argv or on the screen. Restart jared.service afterwards.
set -euo pipefail
[ "$(id -u)" != 0 ] || { echo "ABORT: run as the service user, not as root"; exit 1; }
[ ! -t 0 ] || { echo "ABORT: pipe the token in, do not type it"; exit 1; }
umask 077
D=${XDG_CONFIG_HOME:-$HOME/.config}/jared
mkdir -p "$D"
T=$(head -c 200 | tr -d '[:space:]')
case "$T" in ""|*[!A-Za-z0-9_-]*) echo "ABORT: that is not a token"; exit 1;; esac
[ ${#T} -ge 32 ] || { echo "ABORT: that is not a token (too short)"; exit 1; }
printf '%s' "$T" > "$D/paperless-token.new"
chmod 600 "$D/paperless-token.new"
mv "$D/paperless-token.new" "$D/paperless-token"
echo "Stored $D/paperless-token"
