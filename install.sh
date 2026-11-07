#!/bin/sh
# Subplot installer: see tools/install.py. Usage: sudo ./install.sh [/etc/subplot/subplot.conf] | ./install.sh --check [conf]
exec /usr/bin/python3 -I "$(dirname "$(readlink -f "$0")")/tools/install.py" "$@"
