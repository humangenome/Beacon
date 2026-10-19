#!/usr/bin/env python3
"""Prepare and verify the Nexus mirror of a published GitHub release. No API writes.

Nexus carries one file: the Nexus Mods build of the Beacon installer
(BeaconSetup-<version>-nexus.exe, the app without its self-updater, which Nexus Mods
does not allow) from the latest published release on this repository, in the existing
"Beacon app (installer)" file group, with mod-manager downloads off. GitHub users keep
the self-updating BeaconSetup-<version>.exe; the launcher zip stays on GitHub only.
Releases up to 0.3.133 predate the Nexus build and are checked against their one
installer. Every installer, the BeaconSetup-latest.exe copy and the launcher checksum
manifest are checked against the GitHub release before anything is mirrored."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request
import uuid


REPOSITORY = "HumanGenome/Beacon"
GAME = "subnautica2"
MOD_PAGE_ID = "505"
MOD_NAME = "Beacon - Subnautica 2 Dedicated Server"
FILE_GROUP = "Beacon app (installer)"
SUMS_NAME = "checksums-launcher.txt"
LATEST_ALIAS = "BeaconSetup-latest.exe"
# Uploaded by hand when the page was created (2026-10-13), before this mirror existed,
# with no Nexus changelog entry. Every later version must carry one.
HAND_UPLOADED_WITHOUT_CHANGELOG = frozenset({"0.3.132"})
# The last release published before the Nexus build existed.
LAST_WITHOUT_NEXUS_BUILD = (0, 3, 133)
APPROVED_SCANS = ("VERIFIED", "INTERNALLY_VERIFIED", "MANUALLY_VERIFIED")
# File categories listed on the public Files tab. ARCHIVED files sit in the file archive and
# removed ones are gone, so a file only counts as downloadable in one of these.
LISTED_CATEGORIES = ("MAIN", "UPDATE", "OPTIONAL", "OLD_VERSION", "MISCELLANEOUS")
# Nexus field limits. Version, name and changelog: the API's own schema
# (https://api.nexusmods.com/openapi.yaml, CreateModFileVersionRequest and
# AddModChangelogEntriesRequest, read 2026-10-14). Description: the Nexus file edit form
# rejects more than 255 characters (seen 2026-10-14); the API accepted longer ones on
# 2026-10-13 but may adopt the same limit. The description is one short fixed line and
# the release notes go in the changelog.
VERSION_MAX = 20  # well inside Nexus's 50; keeps the description bounded
NAME_MAX = 50
NAME_PATTERN = r"[a-zA-Z0-9 _'().-]+"
DESCRIPTION_MAX = 255
CHANGELOG_MAX = 65535


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected API redirect; credentials were not forwarded")


def nexus(path, payload=None):
    key = os.environ.get("NEXUSMODS_API_KEY", "")
    if not key:
        raise RuntimeError("NEXUSMODS_API_KEY is missing; configure the repository secret")
    request = urllib.request.Request(
        "https://api.nexusmods.com" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"apikey": key, "Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": "Beacon-release-sync"},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Nexus API returned HTTP {error.code} for {path}") from None


def github_release():
    result = subprocess.run(
        ["gh", "api", f"repos/{REPOSITORY}/releases/latest"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def release_version(release, requested=""):
    tag = release.get("tag_name", "")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("Expected a stable vMAJOR.MINOR.PATCH release")
    if release.get("draft", True) or release.get("prerelease", True) or not release.get("published_at"):
        raise ValueError("Only a published stable release can be mirrored")
    if len(tag) - 1 > VERSION_MAX:
        raise ValueError("Release version is longer than the Nexus mirror accepts")
    if requested and requested != tag:
        raise ValueError("Requested release is no longer Latest; refusing to downgrade Nexus")
    return tag[1:]


def release_url(version):
    return f"https://github.com/{REPOSITORY}/releases/tag/v{version}"


def installer_name(version):
    return f"BeaconSetup-{version}.exe"


def nexus_installer_name(version):
    return f"BeaconSetup-{version}-nexus.exe"


def mirror_name(release, version):
    """The installer that goes to Nexus: the Nexus build, or for releases that predate
    it, the one installer they have. A newer release without the Nexus build fails."""
    names = {a["name"] for a in release["assets"]}
    if nexus_installer_name(version) in names:
        return nexus_installer_name(version)
    if tuple(map(int, version.split("."))) <= LAST_WITHOUT_NEXUS_BUILD:
        return installer_name(version)
    raise ValueError(f"Release has no Nexus Mods build ({nexus_installer_name(version)}); refusing to mirror the self-updating installer")


def release_names(version, mirrored):
    names = {installer_name(version), LATEST_ALIAS, f"Beacon-Launcher-Windows-x64-v{version}.zip"}
    return names | {mirrored}


def verify_assets(folder, release, version):
    """Check the mirrored installer and the checksum manifest against the GitHub release.

    The manifest must name exactly the installer, its BeaconSetup-latest.exe copy, the
    launcher zip and (from 0.3.134) the Nexus build. A line repeated with the same digest is
    tolerated; a name listed with two different digests is not. Files that are not mirrored
    are checked by their GitHub digest only. The Nexus build must differ from the GitHub
    installer. Returns the name of the file to mirror."""
    mirrored = mirror_name(release, version)
    installer = installer_name(version)
    assets = {a["name"]: a for a in release["assets"]}
    expected = {}
    for line in (folder / SUMS_NAME).read_text().splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([a-f0-9]{64})\s+\*?([^/\\]+)", line.strip())
        if not match:
            raise ValueError("Malformed release checksum entry")
        if match[2] in expected and expected[match[2]] != match[1]:
            raise ValueError("Conflicting duplicate release checksum entry")
        expected[match[2]] = match[1]
    if set(expected) != release_names(version, mirrored):
        raise ValueError("Release checksum manifest must name exactly the installers, the latest copy and the launcher zip")
    for name in [SUMS_NAME, mirrored]:
        data = (folder / name).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        asset = assets.get(name)
        if not asset or asset.get("digest") != "sha256:" + digest or asset["size"] != len(data):
            raise ValueError(f"GitHub asset digest/size mismatch: {name}")
        if name in expected and expected[name] != digest:
            raise ValueError(f"Release checksum mismatch: {name}")
    for name in release_names(version, mirrored) - {mirrored}:
        asset = assets.get(name)
        if not asset or asset.get("digest") != "sha256:" + expected[name]:
            raise ValueError(f"Release checksum mismatch: {name}")
    if expected[LATEST_ALIAS] != expected[installer]:
        raise ValueError("BeaconSetup-latest.exe is not the same file as the versioned installer")
    if mirrored != installer and expected[mirrored] == expected[installer]:
        raise ValueError("The Nexus build is the same file as the self-updating installer")
    return mirrored


def select_file_group(files):
    """Return the ID of the one existing installer file group. Other groups are left alone."""
    groups = []
    for file in files:
        if file.get("name", "") == FILE_GROUP:
            groups.append(str(file["id"]))
        else:
            print(f"Nexus: other file group ignored ({file.get('name', '')!r})")
    if not groups:
        raise ValueError("Expected the existing installer file group on Nexus")
    if len(groups) > 1:
        raise ValueError("Multiple Nexus installer file groups; review their API IDs")
    if not re.fullmatch(r"[A-Za-z0-9-]+", groups[0]):
        raise ValueError("Unexpected Nexus file ID")
    return groups[0]


def existing_version(versions, version):
    target = tuple(map(int, version.split(".")))
    for item in versions:
        live_version = item.get("version", "")
        if item.get("category") == "main" and re.fullmatch(r"\d+\.\d+\.\d+", live_version):
            if tuple(map(int, live_version.split("."))) > target:
                raise ValueError("Nexus already has a newer active version; refusing to downgrade")
    matches = [v for v in versions if v.get("version") == version]
    if len(matches) > 1:
        raise ValueError("Duplicate versions on Nexus; review instead of uploading another copy")
    if matches and matches[0].get("category") != "main":
        raise ValueError("Matching Nexus version is not an active main file; manual review required")
    return matches[0] if matches else None


def verify_indexed_hash(folder, filename, current):
    # Nexus's legacy hash index identifies the actual stored file. SHA-256 above
    # remains the release integrity check; this MD5 lookup is only index reconciliation.
    digest = hashlib.md5((folder / filename).read_bytes(), usedforsecurity=False).hexdigest()
    matches = nexus(f"/v1/games/{GAME}/mods/md5_search/{digest}.json")
    if not any(
        str(m.get("mod", {}).get("mod_id")) == MOD_PAGE_ID
        and str(m.get("file_details", {}).get("file_id")) == str(current["game_scoped_id"])
        for m in matches
    ):
        raise ValueError("Nexus has not indexed the expected installer; do not upload a duplicate")


def public_download_status(game_id):
    # The legacy v1 mod metadata can lag behind the live page after a v3 upload.
    # GraphQL is a read-only query for the page version and per-file scan status.
    return nexus("/v2/graphql", {
        "query": """query($mod:ID!,$game:ID!) {
            mod(modId:$mod,gameId:$game) { name version status }
            modFiles(modId:$mod,gameId:$game) {
                fileId name version category primary manager requirementsAlert scannedV2
            }
        }""",
        "variables": {"mod": MOD_PAGE_ID, "game": str(game_id)},
    })


def page_files(response):
    """Every file record on the page, newest upload first (Nexus numbers files in upload order)."""
    data = (response or {}).get("data") or {}
    files = [f for f in (data.get("modFiles") or []) if isinstance(f, dict)]
    return sorted(files, key=file_number, reverse=True)


def file_number(file):
    try:
        return int(file.get("fileId"))
    except (TypeError, ValueError):
        return -1


def is_downloadable(file):
    return file.get("scannedV2") in APPROVED_SCANS and file.get("category") in LISTED_CATEGORIES


def shown(value, limit=60):
    """A Nexus field as plain text that cannot break a log line, an annotation or a table cell."""
    text = re.sub(r"[^A-Za-z0-9 _'().-]", "?", "" if value is None else str(value))[:limit]
    return text or "-"


def newest_downloadable(files):
    """One line naming the newest installer a visitor can download from the Files tab."""
    newest = next((f for f in files if f.get("name") == FILE_GROUP and is_downloadable(f)), None)
    if newest is None:
        return "Newest downloadable installer: none (no installer on the Files tab has passed its scan)"
    return (f"Newest downloadable installer: {shown(newest.get('version'))} "
            f"(file {shown(newest.get('fileId'))}, {shown(newest.get('scannedV2'))})")


def report_files(response):
    """Log every file on the page with its scan state, newest upload first, and return the
    same as a step-summary table. A report only: verify_public_downloads decides pass or fail."""
    files = page_files(response)
    rows = []
    for file in files:
        number, name, version, category, scan = (
            shown(file.get(k)) for k in ("fileId", "name", "version", "category", "scannedV2"))
        downloadable = "yes" if is_downloadable(file) else "no"
        print(f"Nexus file {number}: {name} {version}, {category}, {scan}, downloadable: {downloadable}")
        rows.append(f"| {number} | {name} | {version} | {category} | {scan} | {downloadable} |")
    newest = newest_downloadable(files)
    print(f"Nexus: {newest}")
    return "\n".join([
        "### Nexus files on the page", "",
        "| File | Name | Version | Place | Scan | Downloadable |", "|---|---|---|---|---|---|",
        *(rows or ["| - | no file records returned | - | - | - | - |"]), "", newest, ""])


def write_summary(text):
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if text and target:
        with open(target, "a") as stream:
            stream.write(text + "\n")


def verify_public_downloads(response, version, current, tolerate_quarantine=False):
    """Check the page version and the installer download's controls and scan state.

    Returns True when the installer is quarantined and the caller tolerates it. Only read-only
    runs tolerate a quarantine (reported as a warning). Release runs and manual uploads fail
    on it, and a pending or unknown scan fails every run."""
    if response.get("errors"):
        raise ValueError("Nexus page/scan query failed; download status is unverified")
    data = response.get("data") or {}
    mod = data.get("mod") or {}
    if mod.get("name") != MOD_NAME or mod.get("status") != "published":
        raise ValueError("Nexus page is not the expected published mod")
    if mod.get("version") != version:
        raise ValueError("Nexus page version does not match GitHub Latest")
    matches = [f for f in (data.get("modFiles") or [])
               if str(f.get("fileId")) == str(current["game_scoped_id"])]
    if len(matches) != 1:
        raise ValueError("Nexus installer download status is missing or ambiguous")
    file = matches[0]
    if file.get("version") != version or file.get("category") != "MAIN":
        raise ValueError("Nexus installer download is not the expected active version")
    # Legacy manager=1 disables mod-manager downloads, which an installer must have off.
    if file.get("primary") != 1 or file.get("manager") != 1 or file.get("requirementsAlert") != 1:
        raise ValueError("Nexus installer primary/download/requirements controls do not match")
    status = file.get("scannedV2")
    newest = newest_downloadable(page_files(response))
    if status == "QUARANTINED" and tolerate_quarantine:
        print(f"::warning::Nexus installer {version} is quarantined by its virus scan; moderator or "
              f"antivirus review is a manual decision. Do not reupload. {newest}.")
        return True
    if status == "QUARANTINED":
        raise ValueError(f"Nexus installer is quarantined; moderator review required. Do not reupload. {newest}")
    if status not in APPROVED_SCANS:
        raise ValueError(f"Nexus installer has not passed its scan; download remains unverified. {newest}")
    print(f"::notice::Nexus installer {version} scan cleared ({status}); it is the download on the Files tab")
    print("Nexus installer: scan cleared and download controls verified")
    return False


def plain_notes(body):
    """Release notes as plain changelog lines: bullets prefixed with their section, no
    markdown links or code marks, and no hosting footer (it is on the mod page)."""
    lines, component, kind = [], "", ""
    for raw in (body or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line == "---" or line.lower().startswith("hosting:"):
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line).replace("`", "")
        heading = re.fullmatch(r"(#+)\s*(.+)", line)
        if heading:
            if len(heading[1]) <= 2:
                component, kind = heading[2].strip(), ""
            else:
                kind = heading[2].strip().lower()
            continue
        if line[:2] in ("- ", "* "):
            prefix = ", ".join(p for p in (component, kind) if p)
            text = line[2:].strip()
            lines.append(f"{prefix}: {text}" if prefix else text)
        else:
            lines.append(line)
    return "\n".join(lines)


def nexus_description(version):
    """One short fixed line; never the release notes (see DESCRIPTION_MAX)."""
    text = f"Beacon {version} for Windows. Release notes: {release_url(version)}"
    if len(text) > DESCRIPTION_MAX:
        raise ValueError(f"Nexus file description is over {DESCRIPTION_MAX} characters")
    return text


def nexus_changelog(body, version):
    """The release notes as plain lines, cut with a link back only if they pass Nexus's limit."""
    text = plain_notes(body)
    if not text:
        raise ValueError("Release notes are empty; Nexus needs a changelog entry")
    if len(text) <= CHANGELOG_MAX:
        return text
    tail = f"\nFull release notes: {release_url(version)}"
    return text[:CHANGELOG_MAX - len(tail)].rstrip() + tail


def write_outputs(values):
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a") as stream:
        for key, value in values.items():
            delimiter = "value_" + uuid.uuid4().hex
            stream.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["assets", "prepare", "verify"])
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    release = github_release()
    version = release_version(release, os.environ.get("RELEASE_TAG", ""))
    folder = args.directory
    folder.mkdir(parents=True, exist_ok=True)
    if args.mode != "verify":
        for name in [mirror_name(release, version), SUMS_NAME]:
            subprocess.run([
                "gh", "release", "download", release["tag_name"], "--repo", REPOSITORY,
                "--dir", str(folder), "--pattern", name, "--clobber",
            ], check=True, stdout=subprocess.DEVNULL)
    installer = verify_assets(folder, release, version)
    if args.mode == "assets":
        print(f"GitHub {release['tag_name']}: installer, latest copy and checksum manifest verified")
        return
    mod = nexus(f"/v3/games/{GAME}/mods/{MOD_PAGE_ID}")["data"]
    if str(mod["game_scoped_id"]) != MOD_PAGE_ID or mod.get("name") != MOD_NAME:
        raise ValueError("Unexpected Nexus mod identity")
    mod_id = str(mod["id"])
    if not re.fullmatch(r"[A-Za-z0-9-]+", mod_id):
        raise ValueError("Unexpected Nexus mod ID")
    file_id = select_file_group(nexus(f"/v3/mods/{mod_id}/files")["data"]["mod_files"])
    versions = nexus(f"/v3/mod-files/{file_id}/versions")["data"]["versions"]
    current = existing_version(versions, version)
    if len(FILE_GROUP) > NAME_MAX or not re.fullmatch(NAME_PATTERN, FILE_GROUP):
        raise ValueError("Nexus file name is too long or has characters Nexus refuses")
    outputs = {"version": version, "mod_id": mod_id, "file_id": file_id, "filename": installer,
               "display_name": FILE_GROUP,
               "changelog": nexus_changelog(release.get("body"), version),
               "description": nexus_description(version),
               "installer_upload": "false" if current else "true"}
    # Every file's scan state goes to the step summary once per run: from the verify step, or
    # from whichever step fails first (a failed prepare skips verify).
    summary, failed = "", True
    try:
        page = None
        if current or args.mode == "verify":
            page = public_download_status(mod["game_id"])
            summary = report_files(page)
        if not current and args.mode == "verify":
            raise ValueError("Nexus installer version is missing")
        print(f"Nexus installer: {'version present' if current else 'upload required'}")
        if current:
            if not current.get("is_primary"):
                raise ValueError("The installer must be the primary Nexus download")
            # Set only for read-only runs (nexus.yml: the six-hourly check and a manual run without
            # upload): a standing scan quarantine is reported as a warning instead of failing.
            tolerate = os.environ.get("NEXUS_KNOWN_INSTALLER_QUARANTINE") == "1"
            # Quarantined files can disappear from the legacy hash index. Report the
            # actual moderation state before that lookup can obscure it with a 404.
            quarantined = verify_public_downloads(page, version, current, tolerate)
            try:
                verify_indexed_hash(folder, installer, current)
                print("Nexus installer: hash-index verified")
            except (RuntimeError, ValueError):
                if not quarantined:
                    raise
                print("::warning::Nexus installer hash index unavailable while the file is quarantined")
        if args.mode == "verify":
            if version not in HAND_UPLOADED_WITHOUT_CHANGELOG:
                changelogs = nexus(f"/v1/games/{GAME}/mods/{MOD_PAGE_ID}/changelogs.json")
                if version not in changelogs:
                    raise ValueError("Nexus release changelog is missing")
            print("Nexus installer version, primary download, page version and changelog verified")
            print("Check the installer download button on the Files page and download it normally.")
        failed = False
    finally:
        if failed or args.mode == "verify":
            write_summary(summary)
    write_outputs(outputs)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        # Do not include API response bodies, request headers, or credentials in CI logs.
        print(f"Nexus release check failed: {error}", file=sys.stderr)
        sys.exit(1)
