"""Compare wheel bytes with an independent inventory of the tracked source tree."""

import base64
import csv
from email.parser import BytesParser
import hashlib
import io
from pathlib import Path, PurePosixPath
import zipfile


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def inventory_from_paths(source: Path, tracked_paths: list[str]) -> dict[str, str]:
    """All tracked package files count, including future modules and asset types."""
    result = {}
    for name in tracked_paths:
        if not name.startswith("backend/lohra/") or ".." in PurePosixPath(name).parts:
            raise ValueError(f"invalid package path: {name}")
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"package path is not a regular file: {name}")
        result[name.removeprefix("backend/")] = sha256(path.read_bytes())
    if not result:
        raise ValueError("empty tracked package inventory")
    return result


def verify_wheel(wheel: Path, expected: dict[str, str], version: str) -> dict:
    if not expected:
        raise ValueError("empty source inventory cannot verify a wheel")
    with zipfile.ZipFile(wheel) as archive:
        names = [entry.filename for entry in archive.infolist() if not entry.is_dir()]
        if len(names) != len(set(names)):
            raise ValueError("duplicate wheel member")
        files = {name: archive.read(name) for name in names}
    records = [name for name in files if name.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        raise ValueError("wheel must contain one RECORD")
    record = records[0]
    rows = list(csv.reader(io.StringIO(files[record].decode())))
    if any(len(row) != 3 for row in rows):
        raise ValueError("malformed RECORD row")
    if len(rows) != len(files) or {row[0] for row in rows} != set(files):
        raise ValueError("RECORD members differ from archive")
    for name, encoded_hash, size in rows:
        if name == record and not encoded_hash and not size:
            continue
        method, separator, wanted = encoded_hash.partition("=")
        if not separator or method not in {"sha256", "sha384", "sha512"}:
            raise ValueError(f"unsupported RECORD hash: {name}")
        actual = base64.urlsafe_b64encode(hashlib.new(method, files[name]).digest())
        if actual.rstrip(b"=").decode() != wanted or str(len(files[name])) != size:
            raise ValueError(f"RECORD hash/size differs: {name}")
    package = {name: sha256(value) for name, value in files.items() if name.startswith("lohra/")}
    missing = sorted(expected.keys() - package.keys())
    extra = sorted(package.keys() - expected.keys())
    changed = sorted(name for name in expected.keys() & package.keys() if expected[name] != package[name])
    if missing:
        raise ValueError(f"missing package files: {missing}")
    if extra:
        raise ValueError(f"unexpected package files: {extra}")
    if changed:
        raise ValueError(f"package bytes differ from source: {changed}")
    metadata_name = record.removesuffix("RECORD") + "METADATA"
    metadata = BytesParser().parsebytes(files[metadata_name])
    if metadata["Name"] != "lohra" or metadata["Version"] != version:
        raise ValueError("wheel distribution name/version differs from source")
    return {"package_files": len(package), "record_entries": len(rows),
            "version": metadata["Version"], "requires_dist": metadata.get_all("Requires-Dist", []),
            "wheel_sha256": sha256(wheel.read_bytes()), "wheel_bytes": wheel.stat().st_size}
