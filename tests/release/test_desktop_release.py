"""Portable regressions for the native desktop artifact contract."""

import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from scripts.package_windows_release import ReleaseError, package_release

from tests.release.verify_artifact import ROOT, project_version, verify_artifact


@pytest.fixture
def windows_build(tmp_path: Path) -> Path:
    build = tmp_path / "windows-build"
    build.mkdir()
    (build / "Election Desk.exe").write_bytes(b"standalone executable fixture")
    return build


@pytest.fixture
def windows_archive(windows_build: Path) -> Path:
    return package_release(
        windows_build,
        windows_build / "release",
        release_version=project_version(ROOT),
        backend_release="fixture-backend",
        source_root=ROOT,
        build={"native_windows_build": "Windows_x64"},
    )


def test_delivered_windows_zip_matches_the_standalone_build_and_receipts(
    windows_build: Path, windows_archive: Path
) -> None:
    assert verify_artifact(ROOT, windows_build, "windows", "x64") == windows_archive
    prefix = f"Election-Desk-{project_version(ROOT)}"
    with ZipFile(windows_archive) as delivered:
        assert delivered.read(f"{prefix}/Election Desk.exe") == b"standalone executable fixture"
        identity = json.loads(delivered.read(f"{prefix}/RELEASE.json"))
        instructions = delivered.read(f"{prefix}/README.txt").decode()
    assert identity["bundle_mode"] == "onefile"
    assert identity["backend_pair"] == {
        "backend_release": "fixture-backend",
        "windows_smoke": "INCOMPLETE",
    }
    assert "--server" in instructions and "--ca" in instructions
    assert "recovery journal" in instructions


@pytest.mark.parametrize(
    "extra_file", [".env", "operational-commands/pending.json", "_internal/Qt.dll"]
)
def test_windows_packaging_refuses_private_state_and_sidecar_runtime(
    windows_build: Path, extra_file: str
) -> None:
    extra = windows_build / extra_file
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("private fixture data", encoding="utf-8")
    release = windows_build / "release"

    with pytest.raises(ReleaseError, match="only the onefile Election Desk.exe"):
        package_release(
            windows_build,
            release,
            release_version=project_version(ROOT),
            backend_release="fixture-backend",
            source_root=ROOT,
        )

    assert not release.exists()
    assert extra.read_text(encoding="utf-8") == "private fixture data"


def test_release_verification_rejects_a_changed_zip(
    windows_build: Path, windows_archive: Path
) -> None:
    with windows_archive.open("ab") as payload:
        payload.write(b"changed after packaging")

    with pytest.raises(ReleaseError, match="ZIP does not match its release manifest"):
        verify_artifact(ROOT, windows_build, "windows", "x64")


def test_release_verification_rejects_a_changed_manifest_checksum(
    windows_build: Path, windows_archive: Path
) -> None:
    checksums = windows_archive.with_name("SHA256SUMS")
    zip_line, _ = checksums.read_text(encoding="utf-8").splitlines()
    checksums.write_text(f"{zip_line}\n{'0' * 64}  release-manifest.json\n", encoding="utf-8")

    with pytest.raises(ReleaseError, match="SHA256SUMS does not match"):
        verify_artifact(ROOT, windows_build, "windows", "x64")
