import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import install_macos_app


class InstallMacOSAppTests(unittest.TestCase):
    def test_build_info_plist_contains_required_bundle_keys(self):
        plist = install_macos_app.build_info_plist()
        self.assertEqual("PRTG Monitor TUI", plist["CFBundleName"])
        self.assertEqual("PRTG Monitor TUI", plist["CFBundleDisplayName"])
        self.assertEqual("com.prtg.monitor.tui", plist["CFBundleIdentifier"])
        self.assertEqual("prtg-monitor-tui", plist["CFBundleExecutable"])
        self.assertEqual("AppIcon", plist["CFBundleIconFile"])
        self.assertEqual("APPL", plist["CFBundlePackageType"])
        self.assertTrue(plist["NSHighResolutionCapable"])

    def test_launcher_scripts_are_portable_without_hardcoded_checkout_paths(self):
        launcher = install_macos_app.build_macos_launcher_script()
        self.assertTrue(launcher.startswith("#!/bin/sh\n"))
        self.assertIn("open -a Terminal", launcher)
        self.assertIn("launch_tui.command", launcher)
        self.assertNotIn("/Users/", launcher)

        cmd_script = install_macos_app.build_terminal_command_script("/usr/bin/python3")
        self.assertTrue(cmd_script.startswith("#!/bin/sh\n"))
        self.assertIn("/usr/bin/python3", cmd_script)
        self.assertIn("$SCRIPT_DIR/prtg_tui.py", cmd_script)
        self.assertNotIn("/Users/", cmd_script)

    def test_build_app_bundle_creates_complete_self_contained_layout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = os.path.join(tmpdir, "repo")
            dest_dir = os.path.join(tmpdir, "Applications")
            os.makedirs(repo_dir, exist_ok=True)
            for name in ("prtg_tui.py", "prtg_api.py", "prtg_monitor.py"):
                with open(os.path.join(repo_dir, name), "w", encoding="utf-8") as f:
                    f.write(f"# {name}\n")
            icon_png = os.path.join(tmpdir, "icon.png")
            with open(icon_png, "wb") as f:
                f.write(b"PNG")

            def fake_icns(src, out):
                with open(out, "wb") as f:
                    f.write(b"icns")

            with mock.patch.object(install_macos_app, "generate_icns", side_effect=fake_icns):
                bundle = install_macos_app.build_app_bundle(
                    repo_root=repo_dir,
                    dest_dir=dest_dir,
                    icon_path=icon_png,
                    sign=False,
                    register=False,
                )

            self.assertEqual(os.path.join(dest_dir, "PRTG Monitor TUI.app"), bundle)
            plist_path = os.path.join(bundle, "Contents", "Info.plist")
            with open(plist_path, "rb") as f:
                loaded = plistlib.load(f)
            self.assertEqual("AppIcon", loaded["CFBundleIconFile"])

            exec_path = os.path.join(bundle, "Contents", "MacOS", "prtg-monitor-tui")
            cmd_path = os.path.join(bundle, "Contents", "Resources", "launch_tui.command")
            self.assertEqual(0o755, stat.S_IMODE(os.stat(exec_path).st_mode))
            self.assertEqual(0o755, stat.S_IMODE(os.stat(cmd_path).st_mode))

            for name in ("prtg_tui.py", "prtg_api.py", "prtg_monitor.py", "AppIcon.icns"):
                self.assertTrue(os.path.exists(os.path.join(bundle, "Contents", "Resources", name)))

            # Re-installing replaces previous bundle cleanly
            with mock.patch.object(install_macos_app, "generate_icns", side_effect=fake_icns):
                bundle2 = install_macos_app.build_app_bundle(
                    repo_root=repo_dir,
                    dest_dir=dest_dir,
                    icon_path=icon_png,
                    sign=False,
                    register=False,
                )
            self.assertEqual(bundle, bundle2)

    def test_generate_icns_invokes_sips_and_iconutil(self):
        with self.assertRaises(FileNotFoundError):
            install_macos_app.generate_icns("/nonexistent/icon.png", "/tmp/out.icns")

        with tempfile.TemporaryDirectory() as tmpdir:
            src_png = os.path.join(tmpdir, "src.png")
            out_icns = os.path.join(tmpdir, "AppIcon.icns")
            with open(src_png, "wb") as f:
                f.write(b"PNG")

            with mock.patch("subprocess.run") as mock_run:
                install_macos_app.generate_icns(src_png, out_icns)

            self.assertEqual(len(install_macos_app.ICONSET_SIZES) + 1, mock_run.call_count)
            self.assertEqual("sips", mock_run.call_args_list[0][0][0][0])
            self.assertEqual("iconutil", mock_run.call_args_list[-1][0][0][0])


if __name__ == "__main__":
    unittest.main()
