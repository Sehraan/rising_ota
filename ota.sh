#!/usr/bin/env bash
# ota.sh - publish a ROM build (Infinity X, RisingOS, ...) to the in-app Updater.
# Works on any fresh server: it finds the build zip, the ROM, the version and the
# right OTA repo by itself. It needs only a GitHub token.
#
# Usage:
#   bash ota.sh                    find the newest build zip on this server, publish it
#   bash ota.sh -m "what changed"  same, and write the changelog text
#   bash ota.sh -z /path/rom.zip   use this zip instead of searching
# Options: -z zip  -m message  -v version  -d codename  -r owner/repo  -y (no questions)
# OTA repo is chosen from the zip name: *Infinity* -> sehraan/infinity_ota,
#                                       *Rising*   -> sehraan/rising_ota  (override with -r)
# Search other folders:  ROM_SEARCH="/some/dir /other/dir" bash ota.sh
# Token: export GH_TOKEN=<token> first, or the script asks for it.

set -euo pipefail

DEVICE="spes"
REPO=""
VERSION=""            # empty = read from the build by itself (or force it with -v 4.3)
VERSION_SOURCE=""
ZIP=""; MESSAGE=""; ASSUME_YES=0

while getopts "z:m:v:d:r:y" opt; do
  case "$opt" in
    z) ZIP="$OPTARG" ;;
    m) MESSAGE="$OPTARG" ;;
    v) VERSION="$OPTARG" ;;
    d) DEVICE="$OPTARG" ;;
    r) REPO="$OPTARG" ;;
    y) ASSUME_YES=1 ;;
    *) echo "Unknown option"; exit 1 ;;
  esac
done
shift $((OPTIND - 1))
[ -z "$ZIP" ] && [ -n "${1:-}" ] && ZIP="$1"

# ---- tools -----------------------------------------------------------------
for c in git curl unzip sha256sum stat find; do
  command -v "$c" >/dev/null || { echo "Missing tool: $c"; exit 1; }
done
if ! command -v gh >/dev/null; then
  echo "Installing GitHub CLI (gh)..."
  SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  ($SUDO apt-get update -qq && $SUDO apt-get install -y gh) >/dev/null 2>&1 \
    || { echo "Could not install gh automatically. Install it, then run again."; exit 1; }
fi

# ---- token -----------------------------------------------------------------
if [ -z "${GH_TOKEN:-}" ]; then
  read -rsp "Paste your GitHub token (hidden): " GH_TOKEN; echo
fi
export GH_TOKEN

# ---- find the zip ----------------------------------------------------------
find_zip() {
  local roots=() r
  for r in ${ROM_SEARCH:-/mnt /home /data /workspace /srv /root "$HOME"}; do
    [ -d "$r" ] && roots+=("$r")
  done
  [ "${#roots[@]}" -gt 0 ] || return 0
  find "${roots[@]}" -maxdepth 8 \
    \( -name .repo -o -name .git -o -name node_modules \) -prune -o \
    \( -type f -regex ".*/out/target/product/${DEVICE}/[^/]*\.zip" -printf '%T@ %p\n' \) 2>/dev/null \
    | grep -viE 'target_files|-img|symbols|incremental' | sort -rn | uniq | cut -d' ' -f2- || true
}

if [ -z "$ZIP" ]; then
  echo "Searching this server for a ${DEVICE} build..."
  mapfile -t FOUND < <(find_zip | head -n 5)
  if [ "${#FOUND[@]}" -eq 0 ]; then
    echo "No ${DEVICE} zip found in any out/target/product/${DEVICE} folder."
    echo "Use:  bash ota.sh -z /path/to/rom.zip"
    exit 1
  fi
  echo "Found (newest first):"
  printf '  %s\n' "${FOUND[@]}"
  ZIP="${FOUND[0]}"
fi
[ -f "$ZIP" ] || { echo "Zip not found: $ZIP"; exit 1; }

# ---- ROM version: read it from the build ----------------------------
detect_version() {
  local zip="$1" dir pat f line name
  dir="$(dirname "$zip")"
  for pat in '^ro\.[A-Za-z0-9_.]*(infinity|rising)[A-Za-z0-9_.]*version=' \
             '^ro\.[A-Za-z0-9_.]*(infinity|rising)[A-Za-z0-9_.]*version[A-Za-z0-9_.]*='; do
    for f in system/build.prop system_ext/build.prop product/build.prop vendor/build.prop; do
      [ -f "$dir/$f" ] || continue
      line="$(grep -iE "$pat" "$dir/$f" | head -n1 || true)"
      if [ -n "$line" ]; then
        VERSION="${line#*=}"; VERSION_SOURCE="${line%%=*} in $f"; return 0
      fi
    done
  done
  name="$(basename "$zip")"
  line="$(echo "$name" | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1 || true)"
  if [ -n "$line" ]; then VERSION="$line"; VERSION_SOURCE="zip file name"; return 0; fi
  return 1
}

FILENAME="$(basename "$ZIP")"
TAG="${FILENAME%.zip}"
SIZE="$(stat -c%s "$ZIP")"
if [ -z "$REPO" ]; then
  case "$(echo "$FILENAME" | tr '[:upper:]' '[:lower:]')" in
    *infinity*) REPO="sehraan/infinity_ota" ;;
    *rising*)   REPO="sehraan/rising_ota" ;;
    *) echo "Can't tell which ROM this zip is. Run again with:  -r owner/repo"; exit 1 ;;
  esac
fi
# Use the repo's exact spelling on GitHub (rising_ota vs rising_OTA etc.)
CANON="$(gh repo view "$REPO" --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
case "$CANON" in */*) REPO="$CANON" ;; esac
[ "$SIZE" -lt 2147483648 ] || { echo "Zip is over 2 GB, GitHub Releases can't hold it."; exit 1; }

if [ -z "$VERSION" ]; then
  if ! detect_version "$ZIP"; then
    if [ -t 0 ]; then
      read -rp "Could not detect the version. Type it (for example 4.2): " VERSION
      VERSION_SOURCE="typed by you"
    else
      echo "Could not detect the version. Run again with -v <version>"; exit 1
    fi
  fi
else
  VERSION_SOURCE="-v option"
fi
[ -n "$VERSION" ] || { echo "No version given."; exit 1; }

echo
echo "Will publish: $ZIP ($((SIZE / 1024 / 1024)) MB)"
echo "Version:      $VERSION  (from: $VERSION_SOURCE)"
echo "To repo:      $REPO"
if [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
  read -rp "Is this the right build? [Y/n] " ans
  case "${ans:-y}" in [Yy]*) ;; *) echo "Cancelled."; exit 1 ;; esac
fi

MD5="$(md5sum "$ZIP" | cut -d' ' -f1)"

# Build timestamp: the Updater compares it with the phone's ro.build.date.utc,
# so it comes from the build itself (build.prop first, then the zip's metadata).
TIMESTAMP=""
PROP="$(dirname "$ZIP")/system/build.prop"
[ -f "$PROP" ] && TIMESTAMP="$(grep -m1 '^ro.build.date.utc=' "$PROP" | cut -d= -f2 || true)"
if [ -z "$TIMESTAMP" ]; then
  TIMESTAMP="$(unzip -p "$ZIP" META-INF/com/android/metadata 2>/dev/null | sed -n 's/^post-timestamp=//p' | head -n1 || true)"
fi
if [ -z "$TIMESTAMP" ]; then
  echo "Build timestamp not found, using current time"
  TIMESTAMP="$(date +%s)"
fi

# ---- clone the OTA repo into a temp folder (nothing left behind) -----------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git clone -q "https://x-access-token:${GH_TOKEN}@github.com/${REPO}.git" "$WORK/repo" \
  || { echo "Could not clone $REPO. Check the token and the repo name."; exit 1; }
cd "$WORK/repo"
git config user.name "${REPO%%/*}"
git config user.email "${REPO%%/*}@users.noreply.github.com"

# ---- changelog -------------------------------------------------------------
if [ -n "$MESSAGE" ]; then
  printf '%b\n' "$MESSAGE" > changelog.txt
elif [ ! -f changelog.txt ]; then
  echo "No changelog.txt in the repo. Run again with:  -m \"what changed\""
  exit 1
fi
echo
echo "Changelog that will be shown:"
sed 's/^/  /' changelog.txt
echo

# ---- JSON ------------------------------------------------------------------
URL="https://github.com/${REPO}/releases/download/${TAG}/${FILENAME}"

# 1) If the build generated its own <codename>.json for this exact zip, use it and
#    only swap the download link: that is the ROM's own official layout.
USED_BUILD_JSON=0
BUILD_JSON="$(dirname "$ZIP")/${DEVICE}.json"
if [ -f "$BUILD_JSON" ] && command -v python3 >/dev/null; then
  if python3 - "$BUILD_JSON" "$FILENAME" "$URL" "${DEVICE}.json" 2>/dev/null <<'PY'
import json, sys
src, fname, url, dst = sys.argv[1:5]
d = json.load(open(src))
r = d["response"][0]
if r.get("filename") != fname:
    sys.exit(1)
r["download"] = url
json.dump(d, open(dst, "w"), indent=2)
PY
  then
    USED_BUILD_JSON=1
    echo "Using the JSON your build generated (only the download link was changed)."
  fi
fi

# 2) Otherwise write the standard layout
if [ "$USED_BUILD_JSON" -eq 0 ]; then
  echo "Build made no matching ${DEVICE}.json, using the standard layout."
  cat > "${DEVICE}.json" <<EOF
{
  "response": [
    {
      "filename": "${FILENAME}",
      "download": "${URL}",
      "timestamp": ${TIMESTAMP},
      "md5": "${MD5}",
      "size": ${SIZE},
      "version": "${VERSION}"
    }
  ]
}
EOF
fi

if command -v python3 >/dev/null; then
  python3 -m json.tool "${DEVICE}.json" >/dev/null || { echo "Bad JSON, stopping."; exit 1; }
fi

# ---- upload zip to a GitHub Release, then push the JSON --------------------
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  echo "A release named $TAG already exists. Delete it on GitHub or rebuild, then run again."
  exit 1
fi
echo "Uploading the zip (this can take a few minutes)..."
gh release create "$TAG" "$ZIP" --repo "$REPO" --title "$TAG" --notes-file changelog.txt

git add "${DEVICE}.json" changelog.txt
git diff --cached --quiet || git commit -q -m "${DEVICE}: ${TAG}"
git push -q origin HEAD

BRANCH="$(git branch --show-current)"
echo
echo "Done. Updater JSON: https://raw.githubusercontent.com/${REPO}/${BRANCH}/${DEVICE}.json"
