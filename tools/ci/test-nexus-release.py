#!/usr/bin/env python3
"""Release-safety regressions; no network or publishing calls."""

import contextlib
import copy
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("nexus_release", Path(__file__).with_name("nexus-release.py"))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

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


def write_release_files(folder, version, sums_lines=None):
    """Write an installer fixture plus checksum manifest; return the GitHub asset list."""
    installer = folder / f"BeaconSetup-{version}.exe"
    installer.write_bytes(b"fixture installer " + version.encode())
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    zip_digest = hashlib.sha256(b"fixture zip").hexdigest()
    if sums_lines is None:
        sums_lines = [f"{digest}  BeaconSetup-{version}.exe", f"{digest}  BeaconSetup-latest.exe",
                      f"{zip_digest}  Beacon-Launcher-Windows-x64-v{version}.zip"]
    sums = folder / "checksums-launcher.txt"
    sums.write_text("\n".join(sums_lines) + "\n")
    return [
        {"name": installer.name, "size": installer.stat().st_size, "digest": "sha256:" + digest},
        {"name": "BeaconSetup-latest.exe", "size": installer.stat().st_size, "digest": "sha256:" + digest},
        {"name": f"Beacon-Launcher-Windows-x64-v{version}.zip", "size": 11, "digest": "sha256:" + zip_digest},
        {"name": sums.name, "size": sums.stat().st_size,
         "digest": "sha256:" + hashlib.sha256(sums.read_bytes()).hexdigest()},
    ]


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
        self.md5 = hashlib.md5((self.folder / f"BeaconSetup-{version}.exe").read_bytes()).hexdigest()

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
        self.assertEqual(keys, {"version", "mod_id", "file_id", "display_name", "changelog",
                                "description", "installer_upload"})
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
        self.assertIn("::warning::Nexus installer 0.3.133 is quarantined", log)
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
        (self.folder / f"BeaconSetup-{self.VERSION}.exe").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "digest/size"):
            self.run_mode("verify")
        self.assertEqual(self.calls, [])

    def test_missing_api_key_fails_visibly(self):
        env = {k: v for k, v in os.environ.items() if k != "NEXUSMODS_API_KEY"}
        with patch.dict(os.environ, env, clear=True), self.assertRaisesRegex(RuntimeError, "NEXUSMODS_API_KEY"):
            sync.nexus("/v3/games/subnautica2/mods/505")


if __name__ == "__main__":
    unittest.main()
