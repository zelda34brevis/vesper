"""Strict manifest contract and scope-path helpers shared by vesper applications."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any


ALLOWED_SOURCE_MODES = {"pipeline_url", "job_url_list"}


@dataclass(frozen=True)
class FailedUrlRecord:
    """One requested URL that could not be resolved or downloaded."""

    url: str
    error: str

    @classmethod
    def from_dict(cls, payload: object) -> "FailedUrlRecord":
        data = _require_dict(payload, context="failed_urls[]")
        return cls(
            url=_require_non_empty_str(data, "url", context="failed_urls[]"),
            error=_require_non_empty_str(data, "error", context="failed_urls[]"),
        )


@dataclass(frozen=True)
class ArtifactRecord:
    """One downloaded artifact materialized inside a scope directory."""

    artifact_type: str
    file_name: str
    source_relative_path: str
    relative_path: str
    download_url: str = ""

    @classmethod
    def from_dict(cls, payload: object) -> "ArtifactRecord":
        data = _require_dict(payload, context="runs[].artifacts[]")
        artifact = cls(
            artifact_type=_require_non_empty_str(data, "artifact_type", context="runs[].artifacts[]"),
            file_name=_require_non_empty_str(data, "file_name", context="runs[].artifacts[]"),
            source_relative_path=_require_non_empty_str(data, "source_relative_path", context="runs[].artifacts[]"),
            relative_path=_require_relative_path(data, "relative_path", context="runs[].artifacts[]"),
            download_url=_require_str(data, "download_url", context="runs[].artifacts[]"),
        )
        return artifact


@dataclass(frozen=True)
class RunRecord:
    """One Jenkins run materialized locally for downstream consumers."""

    job_name: str
    job_run_number: str
    run_url: str
    requested_by_urls: list[str]
    run_directory: str
    artifact_count: int
    artifacts: list[ArtifactRecord]

    @classmethod
    def from_dict(cls, payload: object) -> "RunRecord":
        data = _require_dict(payload, context="runs[]")
        run_record = cls(
            job_name=_require_non_empty_str(data, "job_name", context="runs[]"),
            job_run_number=_require_non_empty_str(data, "job_run_number", context="runs[]"),
            run_url=_require_non_empty_str(data, "run_url", context="runs[]"),
            requested_by_urls=_require_non_empty_str_list(data, "requested_by_urls", context="runs[]"),
            run_directory=_require_relative_path(data, "run_directory", context="runs[]"),
            artifact_count=_require_non_negative_int(data, "artifact_count", context="runs[]"),
            artifacts=[ArtifactRecord.from_dict(item) for item in _require_list(data, "artifacts", context="runs[]")],
        )
        run_record.validate()
        return run_record

    def validate(self) -> None:
        if self.artifact_count != len(self.artifacts):
            raise ValueError(
                f"runs[] artifact_count mismatch for run {self.run_url}: expected {self.artifact_count}, found {len(self.artifacts)} artifacts"
            )

        run_directory_prefix = f"{self.run_directory}/"
        for artifact in self.artifacts:
            if artifact.relative_path == self.run_directory:
                raise ValueError(
                    f"Artifact relative_path must point to a file under run_directory for run {self.run_url}: {artifact.relative_path}"
                )
            if not artifact.relative_path.startswith(run_directory_prefix):
                raise ValueError(
                    f"Artifact relative_path must stay under run_directory for run {self.run_url}: {artifact.relative_path}"
                )


@dataclass(frozen=True)
class Manifest:
    """Strict hand-off contract between downloader and reporter."""

    created_at_utc: str
    source_mode: str
    scope_name: str
    requested_urls: list[str]
    resolved_root_run_url: str | None
    build_selector: str
    failed_urls: list[FailedUrlRecord]
    runs: list[RunRecord]

    @classmethod
    def from_dict(cls, payload: object) -> "Manifest":
        data = _require_dict(payload, context="manifest")
        manifest = cls(
            created_at_utc=_require_non_empty_str(data, "created_at_utc", context="manifest"),
            source_mode=_require_non_empty_str(data, "source_mode", context="manifest"),
            scope_name=_require_non_empty_str(data, "scope_name", context="manifest"),
            requested_urls=_require_non_empty_str_list(data, "requested_urls", context="manifest"),
            resolved_root_run_url=_require_optional_non_empty_str(data, "resolved_root_run_url", context="manifest"),
            build_selector=_require_non_empty_str(data, "build_selector", context="manifest"),
            failed_urls=[FailedUrlRecord.from_dict(item) for item in _require_list(data, "failed_urls", context="manifest")],
            runs=[RunRecord.from_dict(item) for item in _require_list(data, "runs", context="manifest")],
        )
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if self.source_mode not in ALLOWED_SOURCE_MODES:
            raise ValueError(
                f"manifest.source_mode must be one of {sorted(ALLOWED_SOURCE_MODES)!r}, got {self.source_mode!r}"
            )
        if self.source_mode == "pipeline_url" and self.resolved_root_run_url is None:
            raise ValueError("manifest.resolved_root_run_url must be present in pipeline_url mode")
        if self.source_mode == "job_url_list" and self.resolved_root_run_url is not None:
            raise ValueError("manifest.resolved_root_run_url must be null in job_url_list mode")
        if not self.runs:
            raise ValueError("manifest.runs must contain at least one successful run")
        for run_record in self.runs:
            run_record.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    def write_text(self, manifest_path: Path) -> Path:
        self.validate()
        target_path = Path(manifest_path).expanduser().resolve()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        return target_path


def manifest_path_for_scope(scope_directory: Path) -> Path:
    """Return the canonical manifest path inside a scope directory."""
    return Path(scope_directory).expanduser().resolve() / "manifest.json"


def load_manifest_file(manifest_path: Path) -> Manifest:
    """Read, parse, and strictly validate a manifest file from disk."""
    target_path = Path(manifest_path).expanduser().resolve()
    raw_payload = json.loads(target_path.read_text(encoding="utf-8"))
    return Manifest.from_dict(raw_payload)


def load_manifest_from_scope(scope_directory: Path) -> tuple[Manifest, Path]:
    """Load the canonical manifest for a scope and return it with the resolved scope path."""
    resolved_scope_directory = Path(scope_directory).expanduser().resolve()
    manifest = load_manifest_file(manifest_path_for_scope(resolved_scope_directory))
    return manifest, resolved_scope_directory


def resolve_run_directory_path(scope_directory: Path, run_record: RunRecord) -> Path:
    """Resolve a run directory path under a scope and reject escaping paths."""
    return _resolve_relative_path(scope_directory=scope_directory, relative_path=run_record.run_directory)


def resolve_artifact_path(scope_directory: Path, artifact_record: ArtifactRecord) -> Path:
    """Resolve an artifact path under a scope and reject escaping paths."""
    return _resolve_relative_path(scope_directory=scope_directory, relative_path=artifact_record.relative_path)


def _resolve_relative_path(scope_directory: Path, relative_path: str) -> Path:
    base_directory = Path(scope_directory).expanduser().resolve()
    target_relative_path = _validate_relative_path_text(relative_path=relative_path, field_label="relative_path")
    resolved_path = (base_directory / target_relative_path).resolve()
    try:
        resolved_path.relative_to(base_directory)
    except ValueError as error_details:
        raise ValueError(f"Resolved path escapes scope directory: {relative_path}") from error_details
    return resolved_path


def _validate_relative_path_text(relative_path: str, field_label: str) -> str:
    normalized_path = PurePosixPath(relative_path)
    if normalized_path.is_absolute():
        raise ValueError(f"{field_label} must be a relative POSIX path, got absolute path {relative_path!r}")
    if not relative_path.strip():
        raise ValueError(f"{field_label} must be a non-empty relative POSIX path")
    if any(path_part in {"", ".", ".."} for path_part in normalized_path.parts):
        raise ValueError(f"{field_label} must not contain empty, '.' or '..' segments: {relative_path!r}")
    return normalized_path.as_posix()


def _require_dict(payload: object, context: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"{context} must be an object")
    return payload


def _require_list(payload: dict[str, Any], field_name: str, context: str) -> list[Any]:
    field_value = payload.get(field_name)
    if not isinstance(field_value, list):
        raise ValueError(f"{context}.{field_name} must be a list")
    return field_value


def _require_str(payload: dict[str, Any], field_name: str, context: str) -> str:
    field_value = payload.get(field_name)
    if not isinstance(field_value, str):
        raise ValueError(f"{context}.{field_name} must be a string")
    return field_value


def _require_non_empty_str(payload: dict[str, Any], field_name: str, context: str) -> str:
    field_value = _require_str(payload, field_name, context).strip()
    if not field_value:
        raise ValueError(f"{context}.{field_name} must be a non-empty string")
    return field_value


def _require_optional_non_empty_str(payload: dict[str, Any], field_name: str, context: str) -> str | None:
    field_value = payload.get(field_name)
    if field_value is None:
        return None
    if not isinstance(field_value, str):
        raise ValueError(f"{context}.{field_name} must be a string or null")
    field_value = field_value.strip()
    if not field_value:
        raise ValueError(f"{context}.{field_name} must be a non-empty string when provided")
    return field_value


def _require_non_empty_str_list(payload: dict[str, Any], field_name: str, context: str) -> list[str]:
    field_value = _require_list(payload, field_name, context)
    if not field_value:
        raise ValueError(f"{context}.{field_name} must contain at least one item")
    normalized_items: list[str] = []
    for item_index, item_value in enumerate(field_value):
        if not isinstance(item_value, str) or not item_value.strip():
            raise ValueError(f"{context}.{field_name}[{item_index}] must be a non-empty string")
        normalized_items.append(item_value)
    return normalized_items


def _require_non_negative_int(payload: dict[str, Any], field_name: str, context: str) -> int:
    field_value = payload.get(field_name)
    if isinstance(field_value, bool) or not isinstance(field_value, int) or field_value < 0:
        raise ValueError(f"{context}.{field_name} must be a non-negative integer")
    return field_value


def _require_relative_path(payload: dict[str, Any], field_name: str, context: str) -> str:
    field_value = _require_str(payload, field_name, context).strip()
    if not field_value:
        raise ValueError(f"{context}.{field_name} must be a non-empty relative POSIX path")
    return _validate_relative_path_text(relative_path=field_value, field_label=f"{context}.{field_name}")
