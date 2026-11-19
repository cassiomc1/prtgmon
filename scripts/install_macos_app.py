#!/usr/bin/env python3
import argparse
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile

APP_NAME = "PRTG Monitor TUI"
BUNDLE_ID = "com.prtg.monitor.tui"
EXECUTABLE_NAME = "prtg-monitor-tui"
APP_VERSION = "1.0.0"
LSREGISTER_BIN = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)
ICONSET_SIZES = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)


def build_info_plist(
    app_name=APP_NAME,
    bundle_id=BUNDLE_ID,
    executable=EXECUTABLE_NAME,
    version=APP_VERSION,
):
    """Builds the Info.plist dictionary for the macOS app bundle."""
    return {
        "CFBundleName": app_name,
        "CFBundleDisplayName": app_name,
        "CFBundleIdentifier": bundle_id,
        "CFBundleVersion": version,
        "CFBundleShortVersionString": version,
        "CFBundlePackageType": "APPL",
        "CFBundleSignature": "????",
        "CFBundleExecutable": executable,
        "CFBundleIconFile": "AppIcon",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
    }


def build_macos_launcher_script():
    """Returns the bundle executable script that opens the TUI in Terminal.app."""
    return (
        "#!/bin/sh\n"
        'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'APP_RESOURCES=$(CDPATH= cd -- "$SCRIPT_DIR/../Resources" && pwd)\n'
        'exec /usr/bin/open -a Terminal "$APP_RESOURCES/launch_tui.command"\n'
    )


def build_terminal_command_script(python_bin="/usr/bin/python3"):
    """Returns the .command script executed inside Terminal.app."""
    quoted_python = shlex.quote(python_bin)
    return (
        "#!/bin/sh\n"
        'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
        'export TERM="${TERM:-xterm-256color}"\n'
        f'{quoted_python} "$SCRIPT_DIR/prtg_tui.py" "$@"\n'
        "status=$?\n"
        'if [ "$status" -ne 0 ]; then\n'
        '    echo ""\n'
        '    echo "PRTG Monitor TUI exited with code $status."\n'
        '    printf "Press Return to close..."\n'
        "    read -r _\n"
        "fi\n"
        'exit "$status"\n'
    )


def generate_icns(source_png, output_icns):
    """Generates a multi-resolution macOS .icns icon file from a PNG image."""
    if not os.path.exists(source_png):
        raise FileNotFoundError(f"Source icon PNG not found: {source_png}")

    output_dir = os.path.dirname(os.path.abspath(output_icns))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="prtg_icon_") as tmpdir:
        iconset_dir = os.path.join(tmpdir, "AppIcon.iconset")
        os.makedirs(iconset_dir, exist_ok=True)

        for filename, size in ICONSET_SIZES:
            dest_png = os.path.join(iconset_dir, filename)
            subprocess.run(
                ["sips", "-z", str(size), str(size), source_png, "--out", dest_png],
                check=True,
                capture_output=True,
                text=True,
            )

        subprocess.run(
            ["iconutil", "-c", "icns", iconset_dir, "-o", output_icns],
            check=True,
            capture_output=True,
            text=True,
        )


BUNDLED_MODULES = ("prtg_tui.py", "prtg_api.py", "prtg_monitor.py")


def build_app_bundle(
    repo_root,
    dest_dir,
    icon_path,
    app_name=APP_NAME,
    python_bin="/usr/bin/python3",
    sign=True,
    register=True,
):
    """Builds and installs a self-contained macOS .app bundle for PRTG Monitor TUI."""
    repo_root = os.path.abspath(repo_root)
    dest_dir = os.path.abspath(os.path.expanduser(dest_dir))
    icon_path = os.path.abspath(os.path.expanduser(icon_path))

    for filename in BUNDLED_MODULES:
        src_file = os.path.join(repo_root, filename)
        if not os.path.exists(src_file):
            raise FileNotFoundError(f"Required source file not found: {src_file}")

    os.makedirs(dest_dir, exist_ok=True)
    dest_bundle = os.path.join(dest_dir, f"{app_name}.app")

    with tempfile.TemporaryDirectory(prefix="prtgmon_app_") as tmpdir:
        staged_bundle = os.path.join(tmpdir, f"{app_name}.app")
        contents_dir = os.path.join(staged_bundle, "Contents")
        macos_dir = os.path.join(contents_dir, "MacOS")
        resources_dir = os.path.join(contents_dir, "Resources")

        os.makedirs(macos_dir, exist_ok=True)
        os.makedirs(resources_dir, exist_ok=True)

        plist_path = os.path.join(contents_dir, "Info.plist")
        with open(plist_path, "wb") as f:
            plistlib.dump(build_info_plist(app_name=app_name), f)

        launcher_path = os.path.join(macos_dir, EXECUTABLE_NAME)
        with open(launcher_path, "w", encoding="utf-8") as f:
            f.write(build_macos_launcher_script())
        os.chmod(launcher_path, 0o755)

        command_path = os.path.join(resources_dir, "launch_tui.command")
        with open(command_path, "w", encoding="utf-8") as f:
            f.write(build_terminal_command_script(python_bin=python_bin))
        os.chmod(command_path, 0o755)

        for filename in BUNDLED_MODULES:
            src_file = os.path.join(repo_root, filename)
            dst_file = os.path.join(resources_dir, filename)
            shutil.copy2(src_file, dst_file)
            os.chmod(dst_file, 0o755)

        generate_icns(icon_path, os.path.join(resources_dir, "AppIcon.icns"))

        if os.path.exists(dest_bundle):
            shutil.rmtree(dest_bundle)
        shutil.copytree(staged_bundle, dest_bundle, symlinks=True)

    if sign:
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", dest_bundle],
            check=True,
            capture_output=True,
            text=True,
        )

    if register:
        subprocess.run(["touch", dest_bundle], check=True)
        if os.path.exists(LSREGISTER_BIN):
            subprocess.run(
                [LSREGISTER_BIN, "-f", dest_bundle],
                check=True,
                capture_output=True,
                text=True,
            )

    return dest_bundle


def build_arg_parser():
    default_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_icon = os.path.join(default_repo, "assets", "prtg-monitor-tui.png")

    parser = argparse.ArgumentParser(
        description="Package and install PRTG Monitor TUI as a macOS .app bundle."
    )
    parser.add_argument(
        "--dest",
        default="/Applications",
        help="Destination Applications folder (default: /Applications)",
    )
    parser.add_argument(
        "--name",
        default=APP_NAME,
        help=f"Application bundle name without .app suffix (default: {APP_NAME})",
    )
    parser.add_argument(
        "--icon",
        default=default_icon,
        help="Path to source PNG icon",
    )
    parser.add_argument(
        "--python",
        default="/usr/bin/python3",
        help="Python interpreter path embedded in the launcher (default: /usr/bin/python3)",
    )
    parser.add_argument(
        "--no-sign",
        action="store_true",
        help="Skip ad-hoc codesign step.",
    )
    parser.add_argument(
        "--no-register",
        action="store_true",
        help="Skip LaunchServices registration.",
    )
    return parser


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    try:
        bundle_path = build_app_bundle(
            repo_root=repo_root,
            dest_dir=args.dest,
            icon_path=args.icon,
            app_name=args.name,
            python_bin=args.python,
            sign=not args.no_sign,
            register=not args.no_register,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"Failed to build/install macOS app bundle: {e}", file=sys.stderr)
        return 1

    print(f"Installed macOS app bundle: {bundle_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
