"""Check a native desktop build's distribution archive before upload."""

import argparse
import hashlib
import json
import plistlib
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from scripts.package_windows_release import ReleaseError, source_identity

ROOT = Path(__file__).resolve().parents[2]


def file_sha256(path: Path) -> str:
    with path.open("rb") as payload:
        return hashlib.file_digest(payload, "sha256").hexdigest()


def project_version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as source:
        return tomllib.load(source)["project"]["version"]


def verify_artifact(root: Path, build: Path, target: str, architecture: str) -> Path:
    version = project_version(root)
    release = build / "release"
    archive = release / f"Election-Desk-{version}-{target}-{architecture}.zip"
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    format_name, platform_name = (
        ("election-windows-release-v1", "Windows11_x64")
        if target == "windows"
        else ("election-macos-release-v1", f"macos-{architecture}")
    )
    if (manifest["format"], manifest["platform"], manifest["release_version"]) != (
        format_name,
        platform_name,
        version,
    ):
        raise ReleaseError("Release manifest version or target differs from the build")
    if manifest["source"] != source_identity(root):
        raise ReleaseError("Release manifest does not identify the current build inputs")

    zip_digest = file_sha256(archive)
    if manifest["archive"] != {
        "filename": archive.name,
        "size_bytes": archive.stat().st_size,
        "sha256": zip_digest,
    }:
        raise ReleaseError("Distribution ZIP does not match its release manifest")
    expected_checksums = [
        f"{zip_digest}  {archive.name}",
        f"{file_sha256(manifest_path)}  {manifest_path.name}",
    ]
    if (release / "SHA256SUMS").read_text(encoding="utf-8").splitlines() != expected_checksums:
        raise ReleaseError("SHA256SUMS does not match the ZIP and release manifest")

    with ZipFile(archive) as payload:
        names = set(payload.namelist())
        if len(names) != len(payload.infolist()):
            raise ReleaseError("Distribution ZIP contains duplicate entries")
        if target == "windows":
            verify_windows_payload(payload, names, build, version, manifest)
        else:
            verify_macos_payload(payload, names, build, version)
    return archive


def verify_windows_payload(
    payload: ZipFile, names: set[str], build: Path, version: str, manifest: dict
) -> None:
    prefix = f"Election-Desk-{version}"
    executable = "Election Desk.exe"
    if names != {f"{prefix}/{name}" for name in (executable, "RELEASE.json", "README.txt")}:
        raise ReleaseError(
            "Windows ZIP must deliver only the standalone executable and release notes"
        )
    if manifest["bundle_mode"] != "onefile" or (
        manifest["build"]["native_windows_build"] != "Windows_x64"
    ):
        raise ReleaseError("Windows release must come from the native x64 onefile build")
    identity = json.loads(payload.read(f"{prefix}/RELEASE.json"))
    if identity != {name: value for name, value in manifest.items() if name != "archive"}:
        raise ReleaseError("Windows ZIP release identity differs from its manifest")
    runtime = build / executable
    digest = file_sha256(runtime)
    if identity["runtime_files"] != [
        {"path": executable, "size_bytes": runtime.stat().st_size, "sha256": digest}
    ]:
        raise ReleaseError("Windows runtime receipt differs from the native build")
    with payload.open(f"{prefix}/{executable}") as packaged:
        if hashlib.file_digest(packaged, "sha256").hexdigest() != digest:
            raise ReleaseError("Windows ZIP executable differs from the native build")
    payload.read(f"{prefix}/README.txt")


def verify_macos_payload(payload: ZipFile, names: set[str], build: Path, version: str) -> None:
    contents = "Election Desk.app/Contents"
    executable = f"{contents}/MacOS/Election Desk"
    if not payload.getinfo(executable).external_attr >> 16 & stat.S_IXUSR:
        raise ReleaseError("macOS ZIP lost the app executable permission")
    with payload.open(executable) as packaged:
        if hashlib.file_digest(packaged, "sha256").hexdigest() != file_sha256(build / executable):
            raise ReleaseError("macOS ZIP executable differs from the native build")
    info = plistlib.loads(payload.read(f"{contents}/Info.plist"))
    if (info["CFBundleExecutable"], info["CFBundleShortVersionString"]) != (
        "Election Desk",
        version,
    ):
        raise ReleaseError("macOS application bundle executable or version is incorrect")
    if not any(
        name.startswith(contents + "/") and name.endswith("/election/desktop/qml/Main.qml")
        for name in names
    ):
        raise ReleaseError("macOS application bundle is missing the main QML view")
    if payload.testzip() is not None:
        raise ReleaseError("macOS ZIP contains a damaged entry")


def smoke_startup(archive: Path, target: str, version: str) -> None:
    """Start the shipped executable from a fresh extraction without a running backend."""
    with TemporaryDirectory(prefix="election-release-") as directory:
        destination = Path(directory)
        if target == "macos":
            subprocess.run(["/usr/bin/ditto", "-x", "-k", archive, destination], check=True)
            executable = destination / "Election Desk.app/Contents/MacOS/Election Desk"
        else:
            executable = destination / "Election Desk.exe"
            with (
                ZipFile(archive) as payload,
                payload.open(f"Election-Desk-{version}/Election Desk.exe") as source,
                executable.open("wb") as published,
            ):
                shutil.copyfileobj(source, published)
        subprocess.run([executable, "--help"], cwd=destination, check=True, timeout=90)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("windows", "macos"))
    parser.add_argument("--architecture", required=True, choices=("x64", "arm64", "x86_64"))
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    args = parser.parse_args()
    version = project_version(args.source_root)
    build = (
        args.source_root / "dist" / args.platform / f"{version}-{args.architecture}-{args.run_name}"
    )
    try:
        archive = verify_artifact(args.source_root, build, args.platform, args.architecture)
        smoke_startup(archive, args.platform, version)
    except (OSError, ValueError, KeyError, BadZipFile, subprocess.SubprocessError) as error:
        raise SystemExit(f"Release verification failed: {error}") from None
    print(f"Verified native release artifact and command-line startup: {archive.name}")


if __name__ == "__main__":
    main()
