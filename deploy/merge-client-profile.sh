#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 thoge191
# Print <config.toml> with its [cloud.client] table replaced by the one in <client.toml> (R13.3).
#
#   merge-client-profile.sh <config.toml> <client.toml>      merged config on stdout; inputs are not modified
#
# <client.toml> is the local, git-ignored file holding how this service presents itself to the INNR cloud
# (see docs/CLOUD_CLIENT.md). It may contain only the [cloud.client] table. Idempotent: running it again
# on its own output gives the same output. Pure bash + POSIX awk (also works with the awk on a NAS).
# Note: the regexes are awk literals on purpose; "awk -v" would interpret the backslashes.
set -euo pipefail

cfg="${1:?usage: merge-client-profile.sh <config.toml> <client.toml>}"
prof="${2:?usage: merge-client-profile.sh <config.toml> <client.toml>}"
[[ -r "$cfg" && -r "$prof" ]] || { echo "merge-client-profile: cannot read an input file" >&2; exit 1; }

grep -Eq '^\[cloud\.client\][[:space:]]*$' "$prof" || { echo "merge-client-profile: $prof has no [cloud.client] table" >&2; exit 1; }
# any other table header in the profile is refused: this file must not be able to change anything else
if awk '/^\[/ && !/^\[cloud\.client\][[:space:]]*$/ { bad = 1 } END { exit bad ? 0 : 1 }' "$prof"; then
  echo "merge-client-profile: $prof may only contain the [cloud.client] table" >&2
  exit 1
fi

# everything of the config except the old [cloud.client] block: its header up to the next table header, plus the
# comment lines directly above the header (they are part of the block, like the comments of the profile file).
# Trailing blank lines are dropped so that repeated merges do not accumulate empty lines.
awk '
  /^\[cloud\.client\][ \t]*$/ { while (n > 0 && lines[n - 1] ~ /^#/) n--; skip = 1; next }
  /^\[/                       { skip = 0 }
  skip == 0                   { lines[n++] = $0 }
  END { while (n > 0 && lines[n - 1] == "") n--; for (i = 0; i < n; i++) print lines[i] }
' "$cfg"
echo
cat "$prof"
