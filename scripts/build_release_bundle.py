"""Build source-free deployment archives from a tracked-file allowlist."""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from scripts.release_validate import project_version

_ROOT_FILES = frozenset({"docker-compose.yml", ".env.example"})
_CONFIG_FILES = frozenset(
    {
        "config/memory_contracts.toml",
        "config/memory_quality_gates.example.toml",
        "config/memory_quality_gates.toml",
        "config/model_profiles.example.toml",
        "config/persona.md",
        "config/qq_face_map.json",
        "config/system_prompt.example.md",
    }
)
_EMPTY_DIRECTORIES = (
    "data",
    "data/speech/cache",
    "data/speech/genie_data",
    "data/speech/voices",
    "data/speech/japanese_frontend/models",
    "plugins",
    "napcat-data",
    "napcat-config",
    "napcat-plugins",
)
_FORBIDDEN_NAMES = frozenset(
    {
        ".env",
        "model_profiles.toml",
        "system_prompt.md",
        "mcp.json",
        "qq_ai_bot.db",
    }
)


class BundleBuildError(ValueError):
    """Raised when the deployment bundle would violate its allowlist."""


def tracked_files(root: Path) -> set[str]:
    completed = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
    return {item.decode("utf-8") for item in completed.stdout.split(b"\0") if item}


def select_bundle_files(files: Iterable[str], version: str) -> dict[str, str]:
    tracked = set(files)
    release_note = f"docs/releases/v{version}.md"
    required = _ROOT_FILES | _CONFIG_FILES | {release_note}
    missing = sorted(required - tracked)
    if missing:
        raise BundleBuildError(f"required tracked deployment files are missing: {missing}")

    selected = {path: path for path in required - {release_note}}
    selected[release_note] = f"Yuki-{version}-Upgrade.md"
    for path in sorted(tracked):
        pure = PurePosixPath(path)
        if pure.parts[:1] == ("plugins",):
            if "tests" in pure.parts or "__pycache__" in pure.parts or pure.suffix == ".pyc":
                continue
            selected[path] = path
        if path == "data/.gitkeep" or path.startswith("data/speech/japanese_frontend/"):
            if "__pycache__" not in pure.parts and pure.suffix != ".pyc":
                selected[path] = path
    _validate_selected_paths(selected.values())
    return selected


def _validate_selected_paths(paths: Iterable[str]) -> None:
    for path in paths:
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts:
            raise BundleBuildError(f"unsafe bundle path: {path}")
        if any(part in _FORBIDDEN_NAMES for part in pure.parts):
            raise BundleBuildError(f"forbidden bundle path: {path}")
        if "__pycache__" in pure.parts or pure.suffix == ".pyc":
            raise BundleBuildError(f"cache file selected for bundle: {path}")


def build_release_bundle(
    root: Path,
    output_directory: Path,
    version: str,
    *,
    tracked: set[str] | None = None,
) -> list[Path]:
    if project_version(root) != version:
        raise BundleBuildError(
            f"requested bundle version {version} does not match project {project_version(root)}"
        )
    selected = select_bundle_files(tracked if tracked is not None else tracked_files(root), version)
    output_directory.mkdir(parents=True, exist_ok=True)
    archive_root_name = f"yuki-{version}-deploy"
    epoch = int(os.getenv("SOURCE_DATE_EPOCH", "0"))

    with tempfile.TemporaryDirectory(prefix="yuki-release-") as temp_name:
        archive_root = Path(temp_name) / archive_root_name
        archive_root.mkdir()
        for source_path, destination_path in selected.items():
            source = root / source_path
            destination = archive_root / destination_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        for directory in _EMPTY_DIRECTORIES:
            (archive_root / directory).mkdir(parents=True, exist_ok=True)

        zip_path = output_directory / f"{archive_root_name}.zip"
        tar_path = output_directory / f"{archive_root_name}.tar.gz"
        _write_zip(archive_root, zip_path, epoch)
        _write_tar_gz(archive_root, tar_path, epoch)

    compose_asset = output_directory / "docker-compose.yml"
    env_asset = output_directory / ".env.example"
    upgrade_asset = output_directory / f"Yuki-{version}-Upgrade.md"
    shutil.copyfile(root / "docker-compose.yml", compose_asset)
    shutil.copyfile(root / ".env.example", env_asset)
    shutil.copyfile(root / f"docs/releases/v{version}.md", upgrade_asset)
    return [zip_path, tar_path, compose_asset, env_asset, upgrade_asset]


def _write_zip(archive_root: Path, destination: Path, epoch: int) -> None:
    timestamp = max(epoch, 315532800)
    date_time = tuple(__import__("time").gmtime(timestamp)[:6])
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in _archive_paths(archive_root):
            relative = path.relative_to(archive_root.parent).as_posix()
            name = f"{relative}/" if path.is_dir() else relative
            info = zipfile.ZipInfo(name, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = (stat.S_IFDIR | 0o755) if path.is_dir() else (stat.S_IFREG | 0o644)
            info.external_attr = mode << 16
            archive.writestr(info, b"" if path.is_dir() else path.read_bytes())


def _write_tar_gz(archive_root: Path, destination: Path, epoch: int) -> None:
    with destination.open("wb") as raw_handle:
        with gzip.GzipFile(fileobj=raw_handle, mode="wb", mtime=epoch) as gzip_handle:
            with tarfile.open(fileobj=gzip_handle, mode="w") as archive:
                for path in _archive_paths(archive_root):
                    relative = path.relative_to(archive_root.parent).as_posix()
                    info = archive.gettarinfo(str(path), arcname=relative)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = epoch
                    if path.is_dir():
                        archive.addfile(info)
                    else:
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)


def _archive_paths(root: Path) -> list[Path]:
    return [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    assets = build_release_bundle(args.root.resolve(), args.output_dir.resolve(), args.version)
    for asset in assets:
        print(asset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
