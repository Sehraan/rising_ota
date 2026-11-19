#!/usr/bin/env python3
"""
OTA release script for Redmi Note 11 (spes): Project Infinity X + RisingOS.

One file, works for both ROMs and on any server:
  * finds the newest ROM zip anywhere on the server (no hardcoded folder)
  * reads version / build date / build type from the zip file name
  * reads the changelog from changelog(s).md/.txt in your OTA repo
  * uploads zip (+ boot.img) to a GitHub release with curl (progress bar)
  * updates the OTA json in the OTA repo and pushes it
No `gh` needed.

Usage (python3, NOT bash):
    export GH_TOKEN=github_pat_xxx
    python3 ota.py                      # auto-detects Infinity or Rising
    python3 ota.py -m "custom text"     # use this changelog instead of the file
    python3 ota.py --rom rising         # force a ROM
    python3 ota.py --zip /path/rom.zip  # skip searching
    python3 ota.py -y                   # no prompts

Needs: python3, curl, git. Token: classic "repo" scope, or fine-grained
with Contents read+write on the OTA repo.
"""

import argparse
import base64
import glob
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# =========================================================
# CONFIG
# =========================================================

REPO_OWNER = "sehraan"
DEVICE_NAME = "Redmi Note 11"
DEVICE_CODENAME = "spes"

PROFILES = {
    "infinity": {
        "label": "Project Infinity X",
        "repo": "infinity_ota",
        "name_re": r"^Project_Infinity-X-.*\.zip$",
        "json_names": ["ota-v2.json"],
    },
    "rising": {
        "label": "RisingOS Revived",
        "repo": "rising_OTA",
        "name_re": r"^Rising.*\.zip$",
        "json_names": [f"{DEVICE_CODENAME}.json", "ota.json", "ota-v2.json"],
    },
}

CHANGELOG_NAMES = {
    "changelog.md", "changelog.txt", "changelogs.md", "changelogs.txt",
}

UPLOAD_TIMEOUT = 3600       # max seconds per file upload attempt
STALL_SECONDS = 120         # abort if speed < 1 KB/s for this long
UPLOAD_ATTEMPTS = 3

PRUNE = [".git", ".repo", "proc", "sys", "dev", "node_modules", "ccache"]

TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""

# set once we know which ROM we're publishing
PROFILE = None
REPO = ""


def set_profile(key):
    global PROFILE, REPO
    PROFILE = PROFILES[key]
    PROFILE["key"] = key
    REPO = f"{REPO_OWNER}/{PROFILE['repo']}"


# =========================================================
# HELPERS
# =========================================================

def die(msg):
    print(f"❌ {msg}")
    sys.exit(1)


def confirm(question, assume_yes):
    if assume_yes:
        return True
    return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")


# =========================================================
# GITHUB API (stdlib only)
# =========================================================

def api(method, path, payload=None):
    """Returns (status_code, parsed_json_or_None)."""
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ota-push-script",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"message": body.decode("utf-8", "ignore")[:200]}
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        die(f"Can't reach api.github.com ({e}). "
            "Check this server's internet / IPv6, then retry.")


def check_token():
    if not TOKEN:
        die("No token. Run:  export GH_TOKEN=your_real_token  and try again.")
    print(f"🔑 Checking GitHub token for {REPO}...")
    code, data = api("GET", f"/repos/{REPO}")
    if code == 401:
        die("GitHub says the token is invalid or revoked. Make a new one.")
    if code == 404:
        die(f"Repo {REPO} not found, or the token has no access to it.")
    if code != 200:
        die(f"GitHub API error {code}: {data}")
    if not (data or {}).get("permissions", {}).get("push"):
        die("Token can read the repo but can't write to it. "
            "Needs 'repo' scope (classic) or Contents: Read+write (fine-grained).")
    print("✅ Token OK")


# =========================================================
# FINDING THE ROM ANYWHERE ON THE SERVER
# =========================================================

def match_profile(path):
    """Which ROM does this zip belong to? (None if neither)"""
    name = os.path.basename(path)
    if DEVICE_CODENAME.lower() not in name.lower():
        return None
    for key, p in PROFILES.items():
        if re.match(p["name_re"], name, re.IGNORECASE):
            return key
    return None


def quick_candidates():
    here = os.getcwd()
    dirs = [here]
    dirs += glob.glob(os.path.join(here, "out", "target", "product", "*"))
    dirs += glob.glob(os.path.join(here, "*", "out", "target", "product", "*"))
    found = []
    for d in dirs:
        try:
            for f in os.listdir(d):
                if f.lower().endswith(".zip"):
                    found.append(os.path.join(d, f))
        except OSError:
            pass
    return found


def search_server(roots):
    found = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        print(f"   searching {root} ...")
        cmd = ["find", root, "-xdev"]
        for d in PRUNE:
            cmd += ["-name", d, "-prune", "-o"]
        cmd += ["-type", "f", "-iname", f"*{DEVICE_CODENAME}*.zip", "-print"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        found += [l for l in result.stdout.splitlines() if l.strip()]
        if any(match_profile(f) for f in found):
            break
    return found


def find_zips(search_roots, rom_filter):
    def pick(paths):
        out = []
        for p in dict.fromkeys(os.path.abspath(x) for x in paths):
            key = match_profile(p)
            if key and (not rom_filter or key == rom_filter):
                out.append((p, key))
        out.sort(key=lambda t: os.path.getmtime(t[0]), reverse=True)
        return out

    found = pick(quick_candidates())
    if not found:
        roots = search_roots or ["/mnt", "/home", "/data", "/root", "/"]
        found = pick(search_server(roots))
    return found


# =========================================================
# READING BUILD INFO FROM THE ZIP NAME
# =========================================================

DATE_DMY = re.compile(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{4})(?!\d)")
DATE_YMD = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
VERSION = re.compile(r"[-_](\d+(?:\.\d+)+)(?=[-_.])")
BUILD_TYPE = re.compile(r"(VANILLA|GAPPS|CORE|FOSS|MICROG)", re.IGNORECASE)


def parse_build(path, forced_version=None):
    name = os.path.basename(path)

    version = forced_version
    if not version:
        for m in VERSION.finditer(name):
            if not DATE_DMY.fullmatch(m.group(1)):   # skip dd.mm.yyyy
                version = m.group(1)
                break

    m = DATE_DMY.search(name)
    if m:
        date = f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
    else:
        m = DATE_YMD.search(name)
        if m:
            date = f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
        else:
            date = time.strftime("%d.%m.%Y", time.localtime(os.path.getmtime(path)))

    m = BUILD_TYPE.search(name)
    build_type = m.group(1).upper() if m else ""

    return {"version": version, "date": date, "type": build_type}


def get_file_info(path):
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest(), os.path.getsize(path), int(os.path.getmtime(path))


def find_boot_img(zip_path):
    folder = os.path.dirname(zip_path)
    for name in ("boot.img", "init_boot.img"):
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            return p
    return None


# =========================================================
# CHANGELOG (read from the repo, or -m / --changelog-file)
# =========================================================

def read_changelog_in(folder):
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return "", None
    for f in names:
        if f.lower() in CHANGELOG_NAMES:
            path = os.path.join(folder, f)
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read().strip()
            if text:
                return text, path
    return "", None


def load_changelog(args, repo_path):
    if args.message:
        return args.message, "-m option"
    if args.changelog_file:
        with open(args.changelog_file, encoding="utf-8", errors="replace") as f:
            return f.read().strip(), args.changelog_file
    for folder in (os.getcwd(), repo_path):
        text, path = read_changelog_in(folder)
        if text:
            return text, path
    return "", None


# =========================================================
# RELEASES + UPLOAD (API + curl)
# =========================================================

def find_releases_with_tag(tag):
    """Includes drafts (e.g. leftovers from a killed upload)."""
    code, data = api("GET", f"/repos/{REPO}/releases?per_page=100")
    if code != 200 or not isinstance(data, list):
        return []
    return [r for r in data if r.get("tag_name") == tag]


def delete_release(release):
    api("DELETE", f"/repos/{REPO}/releases/{release['id']}")
    if not release.get("draft"):
        api("DELETE", f"/repos/{REPO}/git/refs/tags/{release['tag_name']}")


def upload_asset(release_id, path, auth_header_file):
    name = os.path.basename(path)
    size_mb = os.path.getsize(path) // (1024 * 1024)
    url = (
        f"https://uploads.github.com/repos/{REPO}/releases/{release_id}/assets"
        f"?name={urllib.parse.quote(name)}"
    )
    resp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".json").name
    print(f"   ⬆️  {name} ({size_mb} MB)")
    cmd = [
        "curl", "-#", "-X", "POST",
        "-H", f"@{auth_header_file}",
        "-H", "Accept: application/vnd.github+json",
        "-H", "Content-Type: application/octet-stream",
        "-T", path,
        "--connect-timeout", "30",
        "--speed-limit", "1024", "--speed-time", str(STALL_SECONDS),
        "--max-time", str(UPLOAD_TIMEOUT),
        "-o", resp_file,
        "-w", "%{http_code}",
        url,
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
    status = result.stdout.strip()
    try:
        with open(resp_file) as f:
            body = f.read()[:300]
    except Exception:
        body = ""
    os.unlink(resp_file)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed (exit {result.returncode}), "
                           "upload stalled or connection dropped")
    if status != "201":
        raise RuntimeError(f"GitHub returned HTTP {status}: {body}")


def publish_release(tag, info, files, changelog):
    notes = (
        f"{PROFILE['label']} {info['version']} for {DEVICE_NAME} ({DEVICE_CODENAME})\n"
        + (f"Release type: {info['type']}\n" if info["type"] else "")
        + f"Build date: {info['date']}\n\n"
        + changelog
    )

    # token goes in a private temp file so it isn't visible in `ps`
    fd, header_file = tempfile.mkstemp(suffix=".hdr")
    os.chmod(header_file, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"Authorization: Bearer {TOKEN}\n")

    try:
        for attempt in range(1, UPLOAD_ATTEMPTS + 1):
            print(f"📦 Attempt {attempt}/{UPLOAD_ATTEMPTS}: creating draft release...")
            code, rel = api("POST", f"/repos/{REPO}/releases", {
                "tag_name": tag,
                "name": f"{PROFILE['label']} {info['version']} - {DEVICE_NAME}",
                "body": notes,
                "draft": True,
            })
            if code != 201:
                die(f"Could not create release ({code}): {rel}")
            try:
                for path in files:
                    upload_asset(rel["id"], path, header_file)
                code, done = api("PATCH", f"/repos/{REPO}/releases/{rel['id']}",
                                 {"draft": False})
                if code != 200:
                    raise RuntimeError(f"Could not publish release ({code}): {done}")
                print("✅ Upload finished and release published")
                return
            except RuntimeError as e:
                print(f"\n❌ Attempt {attempt} failed: {e}")
                delete_release(rel)
                if attempt == UPLOAD_ATTEMPTS:
                    die("All upload attempts failed.")
                time.sleep(10)
    finally:
        os.unlink(header_file)


# =========================================================
# OTA JSON
# =========================================================

# field -> key names to look for in your existing JSON.
# Existing keys are updated in place. Core fields are added if missing.
FIELD_ALIASES = {
    "filename": ["filename", "file_name"],
    "url": ["url", "download", "download_url"],
    "size": ["size"],
    "sha256": ["sha256", "id"],
    "md5": ["md5"],
    "timestamp": ["datetime", "timestamp"],
    "version": ["version"],
    "release_type": ["romtype", "buildtype", "type"],
    "changelog": ["changelog"],
}
# only touched if your JSON already has the key
OPTIONAL_FIELDS = {"md5", "changelog"}


def set_field(entry, field, value):
    aliases = FIELD_ALIASES[field]
    present = [k for k in aliases if k in entry]
    if present:
        for k in present:
            entry[k] = value
    elif field not in OPTIONAL_FIELDS:
        entry[aliases[0]] = value


def pick_json(repo_path, override):
    if override:
        return override
    for n in PROFILE["json_names"]:
        if os.path.exists(os.path.join(repo_path, n)):
            return n
    jsons = [f for f in os.listdir(repo_path) if f.lower().endswith(".json")]
    if len(jsons) == 1:
        return jsons[0]
    if len(jsons) > 1:
        print(f"⚠️  Several json files in the repo: {jsons}. "
              f"Using {PROFILE['json_names'][0]} (new). "
              "Use --json-file NAME to pick the right one.")
    return PROFILE["json_names"][0]


def update_ota_json(json_path, values):
    if os.path.exists(json_path):
        with open(json_path) as f:
            data = json.load(f)
    else:
        data = {"response": [{}]}

    if isinstance(data, dict) and isinstance(data.get("response"), list):
        if not data["response"]:
            data["response"].append({})
        entry = data["response"][0]
    elif isinstance(data, dict):
        entry = data
    else:
        die(f"Unexpected format in {json_path}")

    for field, value in values.items():
        set_field(entry, field, value)

    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# =========================================================
# GIT (plain git + token header, no gh)
# =========================================================

def git(args, cwd=None, check=True, quiet=False):
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    })
    return subprocess.run(
        ["git"] + args, cwd=cwd, env=env, check=check, timeout=300,
        stdout=subprocess.DEVNULL if quiet else None,
    )


def ensure_ota_repo(path):
    if not os.path.isdir(os.path.join(path, ".git")):
        print(f"📥 Cloning {REPO} to {path}...")
        git(["clone", f"https://github.com/{REPO}.git", path])
    git(["checkout", "main"], cwd=path, quiet=True)
    git(["pull", "--rebase"], cwd=path, quiet=True)


def push_json(path, json_name, tag):
    git(["add", json_name], cwd=path)
    commit = git(
        ["-c", f"user.name={REPO_OWNER}",
         "-c", f"user.email={REPO_OWNER}@users.noreply.github.com",
         "commit", "-m", f"OTA update: {tag}"],
        cwd=path, check=False,
    )
    if commit.returncode != 0:
        print("ℹ️ Nothing new to commit.")
    git(["push", "origin", "main"], cwd=path)


# =========================================================
# MAIN
# =========================================================

def upload_and_push():
    parser = argparse.ArgumentParser(
        description="Publish an OTA for Redmi Note 11 (Infinity X or RisingOS)"
    )
    parser.add_argument("--rom", choices=list(PROFILES), default=None,
                        help="force a ROM (default: auto-detect from the zip)")
    parser.add_argument("-m", "--message", default="",
                        help="changelog text (overrides the changelog file)")
    parser.add_argument("--changelog-file", default=None,
                        help="use this changelog file")
    parser.add_argument("--version", default=None,
                        help="override the version read from the zip name")
    parser.add_argument("--json-file", default=None,
                        help="name of the OTA json inside the repo")
    parser.add_argument("--zip", dest="zip_path", default=None,
                        help="exact zip path (skips searching)")
    parser.add_argument("--search-root", action="append", default=[],
                        help="folder to search (repeatable)")
    parser.add_argument("--ota-repo", default=None,
                        help="where to keep the local OTA repo clone")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="no confirmation prompts")
    args = parser.parse_args()

    # ---------- find the zip (and so, the ROM) ----------
    if args.zip_path:
        zip_path = os.path.abspath(args.zip_path)
        if not os.path.isfile(zip_path):
            die(f"Zip not found: {zip_path}")
        key = args.rom or match_profile(zip_path)
        if not key:
            die("Can't tell which ROM this zip is. Add --rom infinity or --rom rising")
    else:
        print(f"🔎 Searching this server for a {DEVICE_CODENAME} build...")
        found = find_zips(args.search_root, args.rom)
        if not found:
            die("No Infinity X / RisingOS zip found. "
                "Use --zip /path/to/rom.zip or --search-root /folder")
        print("Found (newest first):")
        for i, (z, k) in enumerate(found[:10], 1):
            print(f"  {i}. [{PROFILES[k]['label']}] {z}")
        zip_path, key = found[0]
        if len(found) > 1 and not args.yes:
            pick = input(f"Which one? [1-{min(len(found), 10)}, Enter = newest] ").strip()
            if pick.isdigit() and 1 <= int(pick) <= min(len(found), 10):
                zip_path, key = found[int(pick) - 1]

    set_profile(key)
    check_token()

    # ---------- build info from the zip name ----------
    filename = os.path.basename(zip_path)
    info = parse_build(zip_path, args.version)
    if not info["version"]:
        if args.yes:
            die("Couldn't read the version from the zip name. Use --version 4.2")
        info["version"] = input("Couldn't read the version from the file name. "
                                "Version? ").strip() or "unknown"
    boot_path = find_boot_img(zip_path)
    size_mb = os.path.getsize(zip_path) // (1024 * 1024)
    tag = "-".join(x for x in (info["version"], info["date"], info["type"]) if x)

    # ---------- OTA repo + changelog ----------
    ota_repo = args.ota_repo or os.path.expanduser(f"~/{PROFILE['repo']}_clone")
    ensure_ota_repo(ota_repo)
    changelog, changelog_src = load_changelog(args, ota_repo)
    if not changelog:
        print("⚠️  No changelog found (no -m, and no changelog.md/.txt in the repo).")
        changelog = info["version"]
        changelog_src = "version only"
    json_name = pick_json(ota_repo, args.json_file)

    print()
    print(f"ROM:        {PROFILE['label']}")
    print(f"Zip:        {zip_path} ({size_mb} MB)")
    print(f"Version:    {info['version']}  ({info['type'] or 'no type in name'}, "
          f"built {info['date']})")
    print(f"Device:     {DEVICE_NAME} ({DEVICE_CODENAME})")
    print(f"Boot image: {boot_path or 'not found, skipping'}")
    print(f"Tag:        {tag}")
    print(f"To repo:    {REPO}   (json: {json_name})")
    preview = changelog.splitlines()[:6]
    print(f"Changelog:  from {changelog_src}")
    for line in preview:
        print(f"    {line}")
    if len(changelog.splitlines()) > 6:
        print("    ...")
    print()

    if not confirm("Is this right?", args.yes):
        die("Cancelled.")

    # ---------- leftovers with the same tag (incl. drafts) ----------
    old = find_releases_with_tag(tag)
    if old:
        print(f"⚠️  {len(old)} existing release(s) with tag {tag} "
              "(may be leftovers from a failed upload).")
        if not confirm("Delete them and re-upload?", args.yes):
            die("Cancelled.")
        for r in old:
            delete_release(r)

    files = [zip_path] + ([boot_path] if boot_path else [])
    publish_release(tag, info, files, changelog)

    download_url = f"https://github.com/{REPO}/releases/download/{tag}/{filename}"

    # ---------- OTA JSON ----------
    print("🧮 Calculating checksums...")
    sha256, md5, size, timestamp = get_file_info(zip_path)

    json_path = os.path.join(ota_repo, json_name)
    update_ota_json(json_path, {
        "filename": filename,
        "url": download_url,
        "size": size,
        "sha256": sha256,
        "md5": md5,
        "timestamp": timestamp,
        "version": info["version"],
        "release_type": info["type"] or "UNKNOWN",
        "changelog": changelog,
    })

    print("\n📝 Changes to the OTA json:")
    subprocess.run(["git", "diff", "-U0", "--", json_name], cwd=ota_repo)
    if not confirm("Push this to GitHub?", args.yes):
        print(f"Not pushed. The edited file is at {json_path}")
        return

    print("📤 Pushing OTA JSON...")
    push_json(ota_repo, json_name, tag)

    print("✅ OTA update complete!")
    print(f"   {download_url}")


if __name__ == "__main__":
    upload_and_push()
