"""Local model source and runtime provenance, excluding credentials and datasets."""

import hashlib
import importlib.metadata
import zipfile
from pathlib import Path
from uuid import uuid4

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def model_sources() -> list[tuple[str, Path]]:
    sources = [
        ("src/finlab/" + str(path.relative_to(PACKAGE_ROOT)), path)
        for path in sorted(PACKAGE_ROOT.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.suffix in {".py", ".sql", ".yaml"}
    ]
    project_root = PACKAGE_ROOT.parents[1]
    for name in ("pyproject.toml", "uv.lock"):
        path = project_root / name
        if path.is_file():
            sources.append((name, path))
    return sorted(sources)


def code_hash() -> str:
    digest = hashlib.sha256()
    for name, path in model_sources():
        digest.update(name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def runtime_versions() -> dict[str, str]:
    return {
        name: importlib.metadata.version(name)
        for name in ("duckdb", "polars", "numpy", "pyarrow", "pydantic")
    }


def archive_sources(directory: Path, expected_hash: str) -> Path:
    if code_hash() != expected_hash:
        raise RuntimeError(
            "Source changed during report creation; retry against a stable implementation"
        )
    target = directory / "code" / f"{expected_hash}.zip"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("." + target.name + "-" + uuid4().hex + ".tmp")
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for name, path in model_sources():
                bundle.write(path, name)
        if code_hash() != expected_hash:
            temporary.unlink()
            raise RuntimeError("Source changed while archiving model code")
        temporary.replace(target)
    return target
