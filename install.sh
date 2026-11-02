#!/usr/bin/env bash
# Install requirements from the Linux distribution's configured repositories.
set -euo pipefail

check_tools=0
case "${1:-}" in
  "") ;;
  --check-tools) check_tools=1 ;;
  -h|--help)
    printf '%s\n' 'Usage: bash install.sh [--check-tools]' \
      'Default: Python 3.8+ only (no pip packages or lpunpack needed).' \
      '--check-tools: also install erofs-utils and e2fsprogs for optional read-only checks.'
    exit 0 ;;
  *) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
esac
if (( $# > 1 )); then
  printf '%s\n' 'Too many arguments.' >&2
  exit 2
fi
if [[ "$(uname -s)" != Linux ]]; then
  printf '%s\n' 'This installer is for Linux.' >&2
  exit 1
fi

# Some distributions keep filesystem tools outside the ordinary user's PATH.
export PATH="$PATH:/usr/sbin:/sbin"
packages=()
if ! command -v python3 >/dev/null 2>&1 ||
   ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))'; then
  packages+=(python3)
fi
if (( check_tools )); then
  if ! command -v fsck.erofs >/dev/null 2>&1; then packages+=(erofs-utils); fi
  if ! command -v e2fsck >/dev/null 2>&1; then packages+=(e2fsprogs); fi
fi

if (( ${#packages[@]} )); then
  elevate=()
  if (( EUID != 0 )); then
    if ! command -v sudo >/dev/null 2>&1; then
      printf '%s\n' 'sudo is unavailable. Run this installer as root.' >&2
      exit 1
    fi
    elevate=(sudo)
  fi
  printf 'Installing: %s\n' "${packages[*]}"
  if command -v apt-get >/dev/null 2>&1; then
    "${elevate[@]}" apt-get update
    "${elevate[@]}" apt-get install -y "${packages[@]}"
  elif command -v dnf >/dev/null 2>&1; then
    "${elevate[@]}" dnf install -y "${packages[@]}"
  elif command -v pacman >/dev/null 2>&1; then
    # Use the existing package database; do not upgrade the whole system.
    "${elevate[@]}" pacman -S --needed "${packages[@]}"
  elif command -v zypper >/dev/null 2>&1; then
    "${elevate[@]}" zypper --non-interactive install "${packages[@]}"
  elif command -v apk >/dev/null 2>&1; then
    "${elevate[@]}" apk add --no-cache "${packages[@]}"
  else
    printf 'Unsupported package manager. Install manually: %s\n' "${packages[*]}" >&2
    exit 1
  fi
fi

python3 -c 'import sys; assert sys.version_info >= (3, 8), "Python 3.8 or newer is required"'
if (( check_tools )); then
  command -v fsck.erofs >/dev/null
  command -v e2fsck >/dev/null
fi
printf '%s\n' 'Ready. Run in the folder where you want the output:' \
  '  python3 unpack_super.py -s super.img' \
  'Default output: ./super_extracted/'
