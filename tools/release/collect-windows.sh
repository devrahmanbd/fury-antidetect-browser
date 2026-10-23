#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright 2026 Bogdan Shapovalov and the Fury authors
#
# Put everything a Windows release is made of in one obvious place.
#
#     tools/release/collect-windows.sh [version]     # from Git Bash, on Windows
#
# WHY. The two files a release needs come out of the build eight directories
# apart and under names nobody would guess:
#
#   target\release\bundle\nsis\Fury_0.1.0_x64-setup.exe
#   core\src\out\windows-x64.noindex\  ->  packed by pack-core-windows.sh
#
# The first of those is where cargo-tauri puts it, the second is where ninja
# puts it, and neither is anywhere a person would look. Asked to find the
# installer, the honest answer was a path with `bundle\nsis` in it -- and the
# folder was not found, which is a fair outcome for a path like that.
#
# So both are copied here under the names they will carry on the release page,
# and a copy of the installer goes on the Desktop, which is the one place
# guaranteed to be visible the moment somebody connects over RDP.
#
# Copies rather than moves: the build directories stay valid, so a rerun of
# anything that reads them still works.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
version="${1:-}"
if [ -z "$version" ]; then
  version=$(awk '/^\[workspace\.package\]/{f=1;next} /^\[/{f=0} f&&/^version/{gsub(/[^0-9.]/,"");print;exit}' \
            "$here/Cargo.toml")
fi
[ -n "$version" ] || { echo "!! could not read a version -- pass one" >&2; exit 1; }

dest="${FURY_RELEASE_DIR:-/c/Fury-Release}"
mkdir -p "$dest"

say() { printf '  %s\n' "$1"; }

echo "== collecting $version into $(cygpath -w "$dest" 2>/dev/null || echo "$dest")"

# --- the installer ----------------------------------------------------------
# Matched by version rather than by "the newest one": a stale installer from an
# earlier version sits in the same directory -- Fury_0.0.1_x64-setup.exe was
# still there beside Fury_0.1.0 -- and picking the wrong one is a release that
# installs last week's software under this week's name.
nsis="$here/target/release/bundle/nsis/Fury_${version}_x64-setup.exe"
if [ -f "$nsis" ]; then
  cp "$nsis" "$dest/fury-$version-windows-x64-setup.exe"
  say "installer   fury-$version-windows-x64-setup.exe"
else
  say "installer   MISSING -- run: cd desktop && npm run app:build"
fi

# --- the core ---------------------------------------------------------------
# The NEWEST of the candidates, and it must not predate the build it claims to
# package. This used to take the first that existed, with dist-release/ ahead of
# dist/ -- and dist-release/ is where a PAST release was staged while dist/ is
# where pack-core-windows.sh writes. So after a rebase and a fresh pack, the
# collector kept picking a four-week-old core: measured 10.09.2026, three
# releases' worth of "fury-core-<version>-windows-x64.tar.xz" all came out
# byte-identical to the one packed on 16.08, and nothing said so.
#
# Shipping that would have paired a 150 core with a 153 agent -- a browser
# introducing itself as one version while being another, which is the exact
# contradiction the patch series exists to prevent.
core=""
for candidate in "$here/dist/fury-core-windows.tar.xz" "$here/dist-release/fury-core-windows.tar.xz"; do
  [ -f "$candidate" ] || continue
  if [ -z "$core" ] || [ "$candidate" -nt "$core" ]; then
    core="$candidate"
  fi
done

if [ -z "$core" ]; then
  say "core        MISSING -- run: tools/release/pack-core-windows.sh"
else
  built="$here/core/src/out/windows-x64.noindex/chrome.exe"
  if [ -f "$built" ] && [ "$built" -nt "$core" ]; then
    say "core        REFUSED: $(basename "$(dirname "$core")")/$(basename "$core") is older than the"
    say "            build in core/src/out/windows-x64.noindex. Re-pack it first:"
    say "              OUT_DIR=core/src/out/windows-x64.noindex tools/release/pack-core-windows.sh"
    exit 1
  fi
  cp "$core" "$dest/fury-core-$version-windows-x64.tar.xz"
  say "core        fury-core-$version-windows-x64.tar.xz  (from $(basename "$(dirname "$core")")/)"
fi

# --- checksums --------------------------------------------------------------
# So that a file copied between machines can be shown to have arrived intact,
# and so the release page can carry the same numbers.
( cd "$dest" && sha256sum fury-*"$version"* > "SHA256SUMS-$version.txt" 2>/dev/null || true )
say "checksums   SHA256SUMS-$version.txt"

# --- the copy somebody will actually see ------------------------------------
desktop="$USERPROFILE/Desktop"
desktop="${desktop//\\//}"
if [ -d "$desktop" ] && [ -f "$dest/fury-$version-windows-x64-setup.exe" ]; then
  # A short ASCII name, and both halves of that are deliberate.
  #
  # SHORT, because the Desktop truncates: the release name arrived there as
  # "fury-0.1.0-w..." and the version, the platform and the word setup were all
  # in the part that got cut.
  #
  # ASCII, because a Cyrillic name written from here does not survive the trip.
  # A .ps1 saved as UTF-8 without a byte-order mark is read by PowerShell 5.1 as
  # Windows-1252, so "Fury установка" reached the Desktop as
  # "Fury ÑƒÑÑ‚Ð°Ð½Ð¾Ð²ÐºÐ°" -- a thing this repository already documents in
  # tools/verify-windows.ps1 and which I then did anyway.
  #
  # ONE, because a copy per release is not a copy somebody will see: by 0.4.11
  # the Desktop held 67 identical flame icons, and the newest was the one
  # hardest to find. Every installer is still in $dest; the Desktop only gets
  # the latest.
  rm -f "$desktop"/Fury-Setup-*.exe
  cp "$dest/fury-$version-windows-x64-setup.exe" "$desktop/Fury-Setup-$version.exe"
  say "and Fury-Setup-$version.exe is on the Desktop (older copies removed)"
  # Windows caches a file's icon BY PATH. An installer built before the icon was
  # configured leaves that icon behind for every later build written to the same
  # place, which reads as "the logo did not work" when the logo is in the file.
  # ie4uinit refreshes the cache without restarting Explorer.
  ( cd /c/Windows 2>/dev/null && ./System32/ie4uinit.exe -show 2>/dev/null ) || true
fi

echo
ls -la "$dest"
