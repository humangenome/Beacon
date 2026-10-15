#!/usr/bin/env python3
"""Release-safety regressions; no network or publishing calls."""

import contextlib
import copy
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("nexus_release", Path(__file__).with_name("nexus-release.py"))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "nexus.yml"


def parse_outputs(text):
    """GITHUB_OUTPUT heredoc records (key<<delimiter / value / delimiter) as a dict."""
    return {key: value for key, _, value in
            re.findall(r"^(\w+)<<(value_\w+)\n(.*?)\n\2$", text, re.M | re.S)}

NOTES = """## Client

### Fixed

- The World window shows its snapshot list again, with Download.
- Beacon gives you your own [Subnautica 2](https://example.invalid/) settings back.

## Server

### Changed

- Restore and import are refused (HTTP 409); `/api/v1/health` reports this as `world_restore`.

---

Hosting: [Example](https://example.invalid/) runs Beacon for you.
"""


def write_release_files(folder, version, sums_lines=None, nexus=None):
    """Write installer fixtures plus checksum manifest; return the GitHub asset list.

    From 0.3.134 a release also carries the Nexus build (BeaconSetup-<v>-nexus.exe),
    a different file from the self-updating installer."""
    if nexus is None:
        nexus = tuple(map(int, version.split("."))) > sync.LAST_WITHOUT_NEXUS_BUILD
    installer = folder / f"BeaconSetup-{version}.exe"
    installer.write_bytes(b"fixture installer " + version.encode())
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    zip_digest = hashlib.sha256(b"fixture zip").hexdigest()
    nexus_file = folder / f"BeaconSetup-{version}-nexus.exe"
    if nexus:
        nexus_file.write_bytes(b"fixture nexus installer " + version.encode())
    nexus_digest = hashlib.sha256(nexus_file.read_bytes()).hexdigest() if nexus else None
    if sums_lines is None:
        sums_lines = [f"{digest}  BeaconSetup-{version}.exe", f"{digest}  BeaconSetup-latest.exe",
                      f"{zip_digest}  Beacon-Launcher-Windows-x64-v{version}.zip"]
        if nexus:
            sums_lines.append(f"{nexus_digest}  {nexus_file.name}")
    sums = folder / "checksums-launcher.txt"
    sums.write_text("\n".join(sums_lines) + "\n")
    assets = [
        {"name": installer.name, "size": installer.stat().st_size, "digest": "sha256:" + digest},
        {"name": "BeaconSetup-latest.exe", "size": installer.stat().st_size, "digest": "sha256:" + digest},
        {"name": f"Beacon-Launcher-Windows-x64-v{version}.zip", "size": 11, "digest": "sha256:" + zip_digest},
        {"name": sums.name, "size": sums.stat().st_size,
         "digest": "sha256:" + hashlib.sha256(sums.read_bytes()).hexdigest()},
    ]
    if nexus:
        assets.append({"name": nexus_file.name, "size": nexus_file.stat().st_size, "digest": "sha256:" + nexus_digest})
    return assets


class ReleaseSafety(unittest.TestCase):
    def release(self):
        return {"tag_name": "v0.3.133", "draft": False, "prerelease": False, "published_at": "2026-10-20T16:16:31Z"}

    def test_only_published_latest_stable_release_is_accepted(self):
        self.assertEqual(sync.release_version(self.release()), "0.3.133")
        for key, value in [("draft", True), ("prerelease", True), ("published_at", None),
                           ("tag_name", "v0.3.133;echo unsafe"), ("tag_name", "v0.3.134-rc1")]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                sync.release_version({**self.release(), key: value})
        with self.assertRaisesRegex(ValueError, "downgrade"):
            sync.release_version(self.release(), "v0.3.132")

    def test_installer_group_must_exist_once_and_other_groups_are_ignored(self):
        group = {"id": "8151879", "name": "Beacon app (installer)"}
        other = {"id": "9", "name": "Beacon launcher zip"}
        self.assertEqual(sync.select_file_group([group]), "8151879")
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            self.assertEqual(sync.select_file_group([other, group]), "8151879")
        self.assertIn("other file group ignored", log.getvalue())
        for invalid in [[], [other], [group, group],
                        [{"id": "../another-mod", "name": "Beacon app (installer)"}],
                        [{"id": "a", "name": "Beacon app (installer) 2"}]]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError), \
                    contextlib.redirect_stdout(io.StringIO()):
                sync.select_file_group(invalid)

    def test_retry_does_not_duplicate_existing_or_blocked_versions(self):
        current = {"version": "0.3.133", "category": "main"}
        self.assertEqual(sync.existing_version([current], "0.3.133"), current)
        self.assertIsNone(sync.existing_version([current], "0.3.134"))
        for versions in [[current, current], [{**current, "category": "archived"}],
                         [{**current, "category": "removed"}], [{**current, "category": "unknown"}],
                         [{**current, "version": "0.4.0"}]]:
            with self.subTest(versions=versions), self.assertRaises(ValueError):
                sync.existing_version(versions, "0.3.133")

    def test_altered_artifacts_are_rejected_even_with_same_filenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            release = {**self.release(), "assets": write_release_files(folder, "0.3.133")}
            self.assertEqual(sync.verify_assets(folder, release, "0.3.133"), "BeaconSetup-0.3.133.exe")
            (folder / "BeaconSetup-0.3.133.exe").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "digest/size"):
                sync.verify_assets(folder, release, "0.3.133")

    def test_checksum_manifest_names_exactly_three_files_and_conflicts_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            assets = write_release_files(folder, "0.3.133")
            lines = (folder / "checksums-launcher.txt").read_text().splitlines()
            other = "0" * 64
            for sums, message in [(lines[:2], "exactly"), (lines + [f"{other}  extra.zip"], "exactly"),
                                  (lines + [f"{other}  BeaconSetup-0.3.133.exe"], "Conflicting"),
                                  ([f"{other}  BeaconSetup-0.3.133.exe"] + lines, "Conflicting"),
                                  (lines + ["not a checksum line"], "Malformed")]:
                (folder / "checksums-launcher.txt").write_text("\n".join(sums) + "\n")
                digest = hashlib.sha256((folder / "checksums-launcher.txt").read_bytes()).hexdigest()
                assets[3] = {**assets[3], "size": (folder / "checksums-launcher.txt").stat().st_size,
                             "digest": "sha256:" + digest}
                with self.subTest(sums=sums), self.assertRaisesRegex(ValueError, message):
                    sync.verify_assets(folder, {**self.release(), "assets": assets}, "0.3.133")

    def test_identical_repeated_checksum_line_is_tolerated(self):
        # The v0.3.132 manifest lists the installer twice with the same digest.
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_release_files(folder, "0.3.132")
            lines = (folder / "checksums-launcher.txt").read_text().splitlines()
            assets = write_release_files(folder, "0.3.132", lines + [lines[0]])
            self.assertEqual(sync.verify_assets(folder, {**self.release(), "assets": assets}, "0.3.132"),
                             "BeaconSetup-0.3.132.exe")

    def test_latest_copy_and_zip_must_match_the_github_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            assets = write_release_files(folder, "0.3.133")
            for index in (1, 2):
                changed = copy.deepcopy(assets)
                changed[index]["digest"] = "sha256:" + "1" * 64
                with self.subTest(asset=changed[index]["name"]), self.assertRaisesRegex(ValueError, "mismatch"):
                    sync.verify_assets(folder, {**self.release(), "assets": changed}, "0.3.133")
            missing = [a for a in assets if a["name"] != "BeaconSetup-latest.exe"]
            with self.assertRaises(ValueError):
                sync.verify_assets(folder, {**self.release(), "assets": missing}, "0.3.133")
            digest = hashlib.sha256(b"fixture installer 0.3.133").hexdigest()
            stale = [f"{digest}  BeaconSetup-0.3.133.exe", f"{'2' * 64}  BeaconSetup-latest.exe",
                     f"{hashlib.sha256(b'fixture zip').hexdigest()}  Beacon-Launcher-Windows-x64-v0.3.133.zip"]
            assets = write_release_files(folder, "0.3.133", stale)
            assets[1]["digest"] = "sha256:" + "2" * 64
            with self.assertRaisesRegex(ValueError, "same file"):
                sync.verify_assets(folder, {**self.release(), "assets": assets}, "0.3.133")

    def test_hash_match_must_belong_to_exact_mod_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "fixture.exe").write_bytes(b"fixture")
            current = {"game_scoped_id": "1246"}
            good = {"mod": {"mod_id": 505}, "file_details": {"file_id": 1246}}
            with patch.object(sync, "nexus", return_value=[good]):
                sync.verify_indexed_hash(folder, "fixture.exe", current)
            for records in [[], [{**good, "mod": {"mod_id": 1}}],
                            [{**good, "file_details": {"file_id": 1247}}]]:
                with patch.object(sync, "nexus", return_value=records), self.assertRaises(ValueError):
                    sync.verify_indexed_hash(folder, "fixture.exe", current)

    def test_api_redirects_never_forward_credentials(self):
        with self.assertRaisesRegex(RuntimeError, "not forwarded"):
            sync.NoRedirect().redirect_request(None, None, 302, "", {}, "https://unrelated.example")

    def download_status(self):
        # The installer as Nexus shows it on a clean scan, plus an older version entry.
        return {"data": {
            "mod": {"name": sync.MOD_NAME, "version": "0.3.133", "status": "published"},
            "modFiles": [
                {"fileId": 1300, "version": "0.3.133", "category": "MAIN", "primary": 1,
                 "manager": 1, "requirementsAlert": 1, "scannedV2": "VERIFIED"},
                {"fileId": 1246, "version": "0.3.132", "category": "OLD_VERSION", "primary": 0,
                 "manager": 1, "requirementsAlert": 1, "scannedV2": "QUARANTINED"},
            ],
        }}

    def verify_downloads(self, response, tolerate=False):
        with contextlib.redirect_stdout(io.StringIO()) as log:
            result = sync.verify_public_downloads(response, "0.3.133", {"game_scoped_id": "1300"}, tolerate)
        return result, log.getvalue()

    def test_clean_installer_passes_and_old_versions_are_ignored(self):
        for status in ["VERIFIED", "INTERNALLY_VERIFIED", "MANUALLY_VERIFIED"]:
            response = self.download_status()
            response["data"]["modFiles"][0]["scannedV2"] = status
            self.assertEqual(self.verify_downloads(response)[0], False)

    def test_quarantine_or_pending_scan_fails_release_and_manual_runs(self):
        for status in ["QUARANTINED", "QUEUED", "WAITING_REPORT", "NOT_SCANNED",
                       "REPORT_ERROR", "PARTIAL", "TOO_LARGE", "unknown", None]:
            response = self.download_status()
            response["data"]["modFiles"][0]["scannedV2"] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.verify_downloads(response)

    def test_scheduled_check_warns_on_quarantine_only(self):
        response = self.download_status()
        response["data"]["modFiles"][0]["scannedV2"] = "QUARANTINED"
        result, log = self.verify_downloads(response, tolerate=True)
        self.assertTrue(result)
        self.assertIn("::warning::", log)
        for status in ["QUEUED", "NOT_SCANNED", "unknown", None]:
            response["data"]["modFiles"][0]["scannedV2"] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.verify_downloads(response, tolerate=True)

    def test_page_version_and_each_download_control_must_agree(self):
        for field, value in [("version", "0.3.132"), ("status", "hidden"), ("name", "Another mod")]:
            response = self.download_status()
            response["data"]["mod"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify_downloads(response, tolerate=True)
        for field, value in [("version", "0.3.132"), ("category", "OLD_VERSION"),
                             ("primary", 0), ("manager", 0), ("requirementsAlert", 0)]:
            response = self.download_status()
            response["data"]["modFiles"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify_downloads(response, tolerate=True)

    def test_graphql_errors_missing_and_duplicate_file_records_fail_closed(self):
        response = self.download_status()
        missing = copy.deepcopy(response)
        missing["data"]["modFiles"].pop(0)
        duplicate = copy.deepcopy(response)
        duplicate["data"]["modFiles"].append(duplicate["data"]["modFiles"][0])
        for invalid in [{}, {"data": None}, {"errors": [{"message": "unavailable"}], **response},
                        missing, duplicate]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.verify_downloads(invalid, tolerate=True)

    def test_release_notes_become_plain_changelog_lines(self):
        self.assertEqual(sync.plain_notes(NOTES).splitlines(), [
            "Client, fixed: The World window shows its snapshot list again, with Download.",
            "Client, fixed: Beacon gives you your own Subnautica 2 settings back.",
            "Server, changed: Restore and import are refused (HTTP 409); /api/v1/health reports this as world_restore.",
        ])
        self.assertEqual(sync.plain_notes("## Live map\n\nSee your crew.\n\n- One fix"),
                         "See your crew.\nLive map: One fix")
        self.assertEqual(sync.plain_notes("---\nHosting: [x](https://example.invalid/)"), "")


class InstallerMirror(unittest.TestCase):
    """End-to-end prepare/verify runs against a fake Nexus API."""

    VERSION = "0.3.133"
    GROUP, FILE = "8151879", "1300"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.setup_release(self.VERSION)
        self.groups = [{"id": self.GROUP, "name": "Beacon app (installer)"}]
        self.versions = [
            {"version": "0.3.132", "category": "main", "is_primary": False, "game_scoped_id": "1246"},
            {"version": self.VERSION, "category": "main", "is_primary": True, "game_scoped_id": self.FILE},
        ]
        self.scan = "VERIFIED"
        self.indexed = True
        self.changelogs = {self.VERSION: ["notes"]}
        self.calls = []

    def setup_release(self, version):
        self.version = version
        self.release = {
            "tag_name": "v" + version, "draft": False, "prerelease": False,
            "published_at": "2026-10-20T00:00:00Z", "body": NOTES,
            "assets": write_release_files(self.folder, version),
        }
        self.mirrored = sync.mirror_name(self.release, version)
        self.md5 = hashlib.md5((self.folder / self.mirrored).read_bytes()).hexdigest()

    def fake_nexus(self, path, payload=None):
        self.calls.append(path)
        if path == "/v3/games/subnautica2/mods/505":
            return {"data": {"id": "39505109189113", "game_scoped_id": "505", "name": sync.MOD_NAME,
                             "game_id": "9198"}}
        if path == "/v3/mods/39505109189113/files":
            return {"data": {"mod_files": self.groups}}
        if path == f"/v3/mod-files/{self.GROUP}/versions":
            return {"data": {"versions": self.versions}}
        if path == "/v2/graphql":
            current = [v for v in self.versions if v["version"] == self.version]
            file_id = int(current[0]["game_scoped_id"]) if current else 0
            return {"data": {
                "mod": {"name": sync.MOD_NAME, "version": self.version, "status": "published"},
                "modFiles": [{"fileId": file_id, "version": self.version, "category": "MAIN", "primary": 1,
                              "manager": 1, "requirementsAlert": 1, "scannedV2": self.scan}],
            }}
        if path == f"/v1/games/subnautica2/mods/md5_search/{self.md5}.json":
            if not self.indexed:
                raise RuntimeError("Nexus API returned HTTP 404")
            current = [v for v in self.versions if v["version"] == self.version]
            return [{"mod": {"mod_id": 505}, "file_details": {"file_id": int(current[0]["game_scoped_id"])}}]
        if path == "/v1/games/subnautica2/mods/505/changelogs.json":
            return self.changelogs
        raise AssertionError(f"unexpected Nexus call: {path}")

    def run_mode(self, mode, scheduled=False, release_tag=""):
        outputs = self.folder / "github-output"
        outputs.write_text("")
        env = {k: v for k, v in os.environ.items() if k not in ("RELEASE_TAG", "NEXUS_KNOWN_INSTALLER_QUARANTINE")}
        env["GITHUB_OUTPUT"] = str(outputs)
        if scheduled:
            env["NEXUS_KNOWN_INSTALLER_QUARANTINE"] = "1"
        if release_tag:
            env["RELEASE_TAG"] = release_tag
        with patch.dict(os.environ, env, clear=True), \
                patch.object(sync, "github_release", return_value=self.release), \
                patch.object(sync, "nexus", side_effect=self.fake_nexus), \
                patch.object(sync.subprocess, "run") as download, \
                patch.object(sys, "argv", ["nexus-release.py", mode, "--directory", str(self.folder)]), \
                contextlib.redirect_stdout(io.StringIO()) as log:
            sync.main()
        self.assertEqual(download.call_count, 2 if mode == "prepare" else 0)
        text = outputs.read_text()
        keys = {line.split("<<")[0] for line in text.splitlines() if "<<value_" in line}
        return keys, text, log.getvalue()

    def test_verify_passes_on_a_clean_mirrored_installer(self):
        keys, text, log = self.run_mode("verify")
        self.assertEqual(keys, {"version", "mod_id", "file_id", "filename", "display_name", "changelog",
                                "description", "installer_upload"})
        self.assertRegex(text, r"filename<<value_\w+\n%s\n" % re.escape(self.mirrored))
        self.assertRegex(text, r"installer_upload<<value_\w+\nfalse\n")
        self.assertRegex(text, r"display_name<<value_\w+\nBeacon app \(installer\)\n")
        self.assertIn("hash-index verified", log)
        self.assertIn("/v1/games/subnautica2/mods/505/changelogs.json", self.calls)

    def test_prepare_asks_for_upload_when_latest_is_missing(self):
        self.versions = self.versions[:1]
        _, text, _ = self.run_mode("prepare", release_tag="v" + self.VERSION)
        self.assertRegex(text, r"installer_upload<<value_\w+\ntrue\n")
        self.assertRegex(text, r"file_id<<value_\w+\n8151879\n")
        self.assertIn("Server, changed: Restore and import are refused", text)
        self.assertNotIn("Hosting", text)

    def test_verify_fails_when_version_missing(self):
        self.versions = self.versions[:1]
        with self.assertRaisesRegex(ValueError, "installer version is missing"):
            self.run_mode("verify", scheduled=True)

    def test_release_and_manual_verify_fail_on_quarantine(self):
        self.scan = "QUARANTINED"
        with self.assertRaisesRegex(ValueError, "quarantined"):
            self.run_mode("verify")

    def test_scheduled_verify_warns_on_quarantine_and_missing_index(self):
        self.scan, self.indexed = "QUARANTINED", False
        _, _, log = self.run_mode("verify", scheduled=True)
        self.assertIn("::warning::Nexus installer %s is quarantined" % self.VERSION, log)
        self.assertIn("hash index unavailable", log)

    def test_index_failure_on_a_clean_file_still_fails(self):
        self.indexed = False
        with self.assertRaisesRegex(RuntimeError, "404"):
            self.run_mode("verify", scheduled=True)

    def test_scheduled_verify_fails_on_pending_scan(self):
        self.scan = "QUEUED"
        with self.assertRaisesRegex(ValueError, "not passed its scan"):
            self.run_mode("verify", scheduled=True)

    def test_changelog_required_except_for_the_hand_uploaded_version(self):
        self.changelogs = {}
        with self.assertRaisesRegex(ValueError, "changelog is missing"):
            self.run_mode("verify")
        # Today's page: 0.3.132 uploaded by hand, quarantined, no changelog entry.
        self.setup_release("0.3.132")
        self.versions = [{"version": "0.3.132", "category": "main", "is_primary": True, "game_scoped_id": "1246"}]
        self.scan = "QUARANTINED"
        self.calls = []
        _, _, log = self.run_mode("verify", scheduled=True)
        self.assertNotIn("/v1/games/subnautica2/mods/505/changelogs.json", self.calls)
        self.assertIn("changelog verified", log)
        with self.assertRaisesRegex(ValueError, "quarantined"):
            self.run_mode("verify")

    def test_installer_must_be_primary(self):
        self.versions[1]["is_primary"] = False
        with self.assertRaisesRegex(ValueError, "primary"):
            self.run_mode("verify")

    def test_newer_version_on_nexus_refuses_downgrade(self):
        self.versions.append({"version": "0.3.140", "category": "main", "is_primary": False,
                              "game_scoped_id": "1400"})
        with self.assertRaisesRegex(ValueError, "downgrade"):
            self.run_mode("prepare")

    def test_github_integrity_is_checked_before_any_nexus_call(self):
        (self.folder / self.mirrored).write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "digest/size"):
            self.run_mode("verify")
        self.assertEqual(self.calls, [])

    def test_missing_api_key_fails_visibly(self):
        env = {k: v for k, v in os.environ.items() if k != "NEXUSMODS_API_KEY"}
        with patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(RuntimeError, "NEXUSMODS_API_KEY"):
            sync.nexus("/v3/games/subnautica2/mods/505")

    def test_description_is_one_short_line_whatever_the_release_notes(self):
        # Nexus's file form refuses a description over 255 characters (2026-10-14).
        url = f"https://github.com/HumanGenome/Beacon/releases/tag/v{self.VERSION}"
        for body in [NOTES, "x" * 300, "## Server\n\n### Fixed\n\n- " + "long line " * 8000]:
            self.versions = self.versions[:1]
            self.release["body"] = body
            with self.subTest(body_length=len(body)):
                _, text, _ = self.run_mode("prepare")
                outputs = parse_outputs(text)
                self.assertEqual(outputs["description"],
                                 f"Beacon {self.VERSION} for Windows. Release notes: {url}")
                self.assertLessEqual(len(outputs["description"]), 255)
                self.assertLessEqual(len(outputs["changelog"]), sync.CHANGELOG_MAX)
                if len(body) < sync.CHANGELOG_MAX:
                    self.assertEqual(outputs["changelog"], sync.plain_notes(body))
                else:
                    self.assertTrue(outputs["changelog"].endswith(f"Full release notes: {url}"))

    def test_empty_release_notes_fail_before_upload(self):
        self.versions = self.versions[:1]
        self.release["body"] = "---\nHosting: [x](https://example.invalid/)"
        with self.assertRaisesRegex(ValueError, "Release notes are empty"):
            self.run_mode("prepare")

    def test_workflow_sends_only_the_bounded_outputs(self):
        text = WORKFLOW.read_text()
        step = text.split("uses: Nexus-Mods/upload-action@", 1)[1].split("\n      - name:", 1)[0]
        inputs = dict(re.findall(r"^          (\w+): (.*)$", step, re.M))
        for field in ["description", "changelog", "display_name", "version"]:
            self.assertEqual(inputs[field], "${{ steps.prepare.outputs.%s }}" % field)
        self.assertEqual(inputs["filename"], "${{ runner.temp }}/nexus-release/${{ steps.prepare.outputs.filename }}")
        for field in []:
            self.assertEqual(inputs[field], "${{ steps.prepare.outputs.%s }}" % field)
        self.versions = self.versions[:1]
        keys, _, _ = self.run_mode("prepare")
        used = set(re.findall(r"steps\.prepare\.outputs\.(\w+)", text))
        self.assertLessEqual(used, keys, "the workflow reads an output the script never writes")


class NexusBuildMirror(InstallerMirror):
    """From 0.3.134 the mirror uploads the Nexus build, never the self-updating installer."""

    VERSION = "0.3.134"

    def test_the_nexus_build_is_the_file_mirrored(self):
        self.assertEqual(self.mirrored, "BeaconSetup-0.3.134-nexus.exe")
        self.versions = self.versions[:1]
        _, text, _ = self.run_mode("prepare", release_tag="v0.3.134")
        self.assertRegex(text, r"filename<<value_\w+\nBeaconSetup-0\.3\.134-nexus\.exe\n")


class NexusBuildSelection(unittest.TestCase):
    def release(self, version, assets):
        return {"tag_name": "v" + version, "draft": False, "prerelease": False,
                "published_at": "2026-10-20T00:00:00Z", "assets": assets}

    def test_release_without_the_nexus_build_is_refused_from_0_3_134(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            assets = write_release_files(folder, "0.3.134", nexus=False)
            with self.assertRaisesRegex(ValueError, "no Nexus Mods build"):
                sync.verify_assets(folder, self.release("0.3.134", assets), "0.3.134")

    def test_releases_up_to_0_3_133_mirror_their_one_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            assets = write_release_files(folder, "0.3.133")
            self.assertEqual(sync.verify_assets(folder, self.release("0.3.133", assets), "0.3.133"),
                             "BeaconSetup-0.3.133.exe")

    def test_nexus_build_must_differ_from_the_self_updating_installer(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            write_release_files(folder, "0.3.134")
            (folder / "BeaconSetup-0.3.134-nexus.exe").write_bytes((folder / "BeaconSetup-0.3.134.exe").read_bytes())
            same = hashlib.sha256((folder / "BeaconSetup-0.3.134.exe").read_bytes()).hexdigest()
            zip_digest = hashlib.sha256(b"fixture zip").hexdigest()
            lines = [f"{same}  BeaconSetup-0.3.134.exe", f"{same}  BeaconSetup-latest.exe",
                     f"{zip_digest}  Beacon-Launcher-Windows-x64-v0.3.134.zip", f"{same}  BeaconSetup-0.3.134-nexus.exe"]
            assets = write_release_files(folder, "0.3.134", lines, nexus=False)
            (folder / "BeaconSetup-0.3.134-nexus.exe").write_bytes((folder / "BeaconSetup-0.3.134.exe").read_bytes())
            size = (folder / "BeaconSetup-0.3.134-nexus.exe").stat().st_size
            assets.append({"name": "BeaconSetup-0.3.134-nexus.exe", "size": size, "digest": "sha256:" + same})
            with self.assertRaisesRegex(ValueError, "same file"):
                sync.verify_assets(folder, self.release("0.3.134", assets), "0.3.134")

    def test_manifest_must_list_the_nexus_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            assets = write_release_files(folder, "0.3.134")
            lines = (folder / "checksums-launcher.txt").read_text().splitlines()[:3]
            (folder / "checksums-launcher.txt").write_text("\n".join(lines) + "\n")
            sums = folder / "checksums-launcher.txt"
            assets = [a if a["name"] != sums.name else {**a, "size": sums.stat().st_size,
                      "digest": "sha256:" + hashlib.sha256(sums.read_bytes()).hexdigest()} for a in assets]
            with self.assertRaisesRegex(ValueError, "exactly"):
                sync.verify_assets(folder, self.release("0.3.134", assets), "0.3.134")


class NexusFieldLimits(unittest.TestCase):
    """Every text field the upload sends stays inside Nexus's limits, for any accepted version."""

    LONGEST = "999999.999999.999999"

    def release(self, tag):
        return {"tag_name": tag, "draft": False, "prerelease": False, "published_at": "2026-10-20T00:00:00Z"}

    def test_longest_accepted_version_keeps_description_and_name_inside_the_limits(self):
        self.assertEqual(len(self.LONGEST), sync.VERSION_MAX)
        version = sync.release_version(self.release("v" + self.LONGEST))
        description = sync.nexus_description(version)
        self.assertLessEqual(len(description), 255)
        self.assertEqual(sync.DESCRIPTION_MAX, 255)
        # ASCII with nothing Nexus stores HTML-escaped, so the stored length is the sent length.
        self.assertTrue(description.isascii())
        self.assertFalse(set(description) & set("<>&\"'"))
        self.assertLessEqual(len(sync.FILE_GROUP), 50)
        self.assertRegex(sync.FILE_GROUP, r"^[a-zA-Z0-9 _'().-]+$")

    def test_a_longer_version_is_refused_before_anything_is_uploaded(self):
        with self.assertRaisesRegex(ValueError, "longer than"):
            sync.release_version(self.release("v" + self.LONGEST + "9"))

    def test_description_guard_fails_instead_of_sending_a_long_line(self):
        with patch.object(sync, "REPOSITORY", "HumanGenome/" + "x" * 255), \
                self.assertRaisesRegex(ValueError, "over 255"):
            sync.nexus_description("0.3.134")


if __name__ == "__main__":
    unittest.main()
