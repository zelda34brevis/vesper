from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import logging
import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from zipfile import BadZipFile, ZipFile

LOGGER = logging.getLogger(__name__)
BACKTRACE_LOG_BASENAME_PATTERN = re.compile(r"^backtrace_.*\.log$", re.IGNORECASE)
REPORT_TEARDOWN_HOOK_LABEL_PREFIX = "report_test_fixture"


@dataclass
class InputConfig:
    manifest_path: str | None = None
    scope_dir: str | None = None


@dataclass
class ParsingConfig:
    test_identifier_prefix: str = "AUTO-A"
    test_identifier_marker: str = "test_id"
    retain_latest_execution_per_job: bool = True
    partial_mode: str = "warn"


@dataclass
class OutputConfig:
    csv_path: str | None = None
    backtrace_output_directory: str | None = None


@dataclass
class ReporterConfig:
    input: InputConfig = field(default_factory=InputConfig)
    parsing: ParsingConfig = field(default_factory=ParsingConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


@dataclass
class ArtifactRecord:
    artifact_type: str
    file_name: str
    source_relative_path: str
    relative_path: str
    download_url: str = ""


@dataclass
class RunRecord:
    job_name: str
    job_run_number: str
    run_url: str
    requested_by_urls: list[str]
    run_directory: str
    artifact_count: int
    artifacts: list[ArtifactRecord]


@dataclass
class Manifest:
    created_at_utc: str
    source_mode: str
    scope_name: str
    requested_urls: list[str]
    resolved_root_run_url: str | None
    build_selector: str
    failed_urls: list[dict[str, str]]
    runs: list[RunRecord]


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def load_config(config_path: Path) -> ReporterConfig:
    raw_payload = json.loads(config_path.read_text(encoding="utf-8"))
    return ReporterConfig(
        input=InputConfig(**raw_payload.get("input", {})),
        parsing=ParsingConfig(**raw_payload.get("parsing", {})),
        output=OutputConfig(**raw_payload.get("output", {})),
    )


def validate_config(config: ReporterConfig) -> None:
    input_config = config.input
    if bool(input_config.manifest_path) == bool(input_config.scope_dir):
        raise ValueError("Provide exactly one input source: manifest_path or scope_dir")
    if config.parsing.partial_mode not in {"warn", "fail"}:
        raise ValueError("parsing.partial_mode must be 'warn' or 'fail'")


def make_safe_component(raw_value: Any, default_value: str, allow_dots: bool = True) -> str:
    sanitized_value = str(raw_value).strip()
    sanitized_value = re.sub(r"\s+", "_", sanitized_value)
    sanitized_pattern = r"[^A-Za-z0-9._-]" if allow_dots else r"[^A-Za-z0-9_-]"
    sanitized_value = re.sub(sanitized_pattern, "_", sanitized_value)
    strip_chars = "._-" if allow_dots else "_-"
    sanitized_value = re.sub(r"_+", "_", sanitized_value).strip(strip_chars)
    return sanitized_value or default_value


def truncate_component(component_text: str, max_length: int) -> str:
    if len(component_text) <= max_length:
        return component_text
    sha_digest = hashlib.sha1(component_text.encode("utf-8")).hexdigest()[:8]
    prefix_length = max_length - len(sha_digest) - 1
    if prefix_length <= 0:
        return sha_digest[:max_length]
    return f"{component_text[:prefix_length]}-{sha_digest}"


def load_manifest(config: ReporterConfig) -> tuple[Manifest, Path]:
    if config.input.manifest_path:
        manifest_path = Path(config.input.manifest_path).expanduser().resolve()
    else:
        scope_directory = Path(config.input.scope_dir or "").expanduser().resolve()
        manifest_path = scope_directory / "manifest.json"
    scope_dir = manifest_path.parent
    raw_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    runs = [
        RunRecord(
            job_name=run_payload.get("job_name", ""),
            job_run_number=run_payload.get("job_run_number", ""),
            run_url=run_payload.get("run_url", ""),
            requested_by_urls=list(run_payload.get("requested_by_urls", [])),
            run_directory=run_payload.get("run_directory", ""),
            artifact_count=int(run_payload.get("artifact_count", 0)),
            artifacts=[ArtifactRecord(**artifact_payload) for artifact_payload in run_payload.get("artifacts", [])],
        )
        for run_payload in raw_payload.get("runs", [])
    ]
    manifest = Manifest(
        created_at_utc=raw_payload.get("created_at_utc", ""),
        source_mode=raw_payload.get("source_mode", ""),
        scope_name=raw_payload.get("scope_name", ""),
        requested_urls=list(raw_payload.get("requested_urls", [])),
        resolved_root_run_url=raw_payload.get("resolved_root_run_url"),
        build_selector=raw_payload.get("build_selector", ""),
        failed_urls=list(raw_payload.get("failed_urls", [])),
        runs=runs,
    )
    return manifest, scope_dir


def _coerce_non_negative_int(candidate_value) -> int | None:
    if isinstance(candidate_value, bool):
        return None
    if isinstance(candidate_value, int):
        return candidate_value if candidate_value >= 0 else None
    if isinstance(candidate_value, str):
        stripped_value = candidate_value.strip()
        if stripped_value.isdigit():
            return int(stripped_value)
    return None


def _read_rerun_count_from_parameters(parameter_entries) -> int | None:
    if not isinstance(parameter_entries, list):
        return None
    for parameter_entry in parameter_entries:
        if not isinstance(parameter_entry, dict):
            continue
        parameter_label = parameter_entry.get("name")
        if parameter_label not in ("rerun_count", "rerunCount", "retriesCount"):
            continue
        parsed_integer = _coerce_non_negative_int(parameter_entry.get("value"))
        if parsed_integer is not None:
            return parsed_integer
    return None


def _read_rerun_count(test_result_payload) -> int:
    for lookup_key in ("rerun_count", "rerunCount", "retriesCount"):
        parsed_integer = _coerce_non_negative_int(test_result_payload.get(lookup_key))
        if parsed_integer is not None:
            return parsed_integer

    extra_metadata_payload = test_result_payload.get("extra")
    if isinstance(extra_metadata_payload, dict):
        for lookup_key in ("rerun_count", "rerunCount", "retriesCount"):
            parsed_integer = _coerce_non_negative_int(extra_metadata_payload.get(lookup_key))
            if parsed_integer is not None:
                return parsed_integer

    parsed_rerun_count = _read_rerun_count_from_parameters(test_result_payload.get("parameters"))
    if parsed_rerun_count is not None:
        return parsed_rerun_count

    if isinstance(extra_metadata_payload, dict):
        retry_entries = extra_metadata_payload.get("retries")
        if isinstance(retry_entries, list):
            return len(retry_entries)

    retry_entries = test_result_payload.get("retries")
    if isinstance(retry_entries, list):
        return len(retry_entries)

    if test_result_payload.get("retry") is True:
        return 1
    return 0


def _sanitize_testrun_id(raw_testrun_identifier) -> str:
    if not isinstance(raw_testrun_identifier, str):
        return ""
    sanitized_value = raw_testrun_identifier.strip()
    if not sanitized_value:
        return ""
    sanitized_value = re.sub(r"\s+", "_", sanitized_value)
    sanitized_value = re.sub(r"[^A-Za-z0-9_-]", "_", sanitized_value)
    sanitized_value = re.sub(r"_+", "_", sanitized_value).strip("_-")
    return sanitized_value


def _read_testrun_id(test_result_payload, test_case_filename) -> str:
    for lookup_key in ("uid", "historyId", "testCaseId"):
        normalized_value = _sanitize_testrun_id(test_result_payload.get(lookup_key))
        if normalized_value:
            return normalized_value
    return _sanitize_testrun_id(PurePosixPath(test_case_filename).stem) or "unknown_testrun"


def _parse_test_identity(full_name_text, short_name_text) -> tuple[str, str]:
    if full_name_text and "#" in full_name_text:
        source_file_path, test_case_name = full_name_text.rsplit("#", 1)
        return source_file_path, test_case_name
    if full_name_text:
        return full_name_text, short_name_text or full_name_text
    if short_name_text:
        return "", short_name_text
    return "", ""


def _format_utc_timestamp_from_ms(timestamp_ms) -> str:
    if timestamp_ms is None:
        return ""
    formatted_timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    return formatted_timestamp[:-3]


def _read_stop_ms(time_payload: object) -> int | None:
    if not isinstance(time_payload, dict):
        return None
    return _coerce_non_negative_int(time_payload.get("stop"))


def _has_failed_like_status(result_status) -> bool:
    return isinstance(result_status, str) and result_status in {"failed", "broken"}


def _is_setup_failure_reflected_in_test_stage(body_stage_payload) -> bool:
    if not isinstance(body_stage_payload, dict):
        return False
    if not _has_failed_like_status(body_stage_payload.get("status")):
        return False
    step_count = body_stage_payload.get("stepsCount")
    step_entries = body_stage_payload.get("steps")
    steps_present = isinstance(step_entries, list) and len(step_entries) > 0
    return step_count == 0 and not steps_present


def _read_failed_stop_ms_from_stage_tree(stage_payload) -> int | None:
    if not isinstance(stage_payload, dict):
        return None
    child_failure_stops: list[int] = []
    step_entries = stage_payload.get("steps")
    if isinstance(step_entries, list):
        for step_entry in step_entries:
            child_failure_stop_ms = _read_failed_stop_ms_from_stage_tree(step_entry)
            if child_failure_stop_ms is not None:
                child_failure_stops.append(child_failure_stop_ms)
    if child_failure_stops:
        return max(child_failure_stops)
    if not _has_failed_like_status(stage_payload.get("status")):
        return None
    stage_time_payload = stage_payload.get("time")
    return _read_stop_ms(stage_time_payload) if isinstance(stage_time_payload, dict) else None


def _read_failed_stop_ms_from_stages(stage_items) -> int | None:
    if not isinstance(stage_items, list):
        return None
    failed_stop_timestamps: list[int] = []
    for stage_entry in stage_items:
        failed_stop_ms = _read_failed_stop_ms_from_stage_tree(stage_entry)
        if failed_stop_ms is not None:
            failed_stop_timestamps.append(failed_stop_ms)
    if not failed_stop_timestamps:
        return None
    return max(failed_stop_timestamps)


def _collect_failed_stage_names(stage_items) -> list[str]:
    if not isinstance(stage_items, list):
        return []
    failed_stage_names: list[str] = []
    for stage_entry in stage_items:
        if not isinstance(stage_entry, dict):
            continue
        if not _has_failed_like_status(stage_entry.get("status")):
            continue
        stage_label = stage_entry.get("name")
        if isinstance(stage_label, str) and stage_label:
            failed_stage_names.append(stage_label)
    return failed_stage_names


def _has_teardown_publish_failure_only(test_result_payload) -> bool:
    failed_setup_hook_names = _collect_failed_stage_names(test_result_payload.get("beforeStages"))
    if failed_setup_hook_names:
        return False
    failed_teardown_hook_names = _collect_failed_stage_names(test_result_payload.get("afterStages"))
    if not failed_teardown_hook_names:
        return False
    return all(hook_name.startswith(REPORT_TEARDOWN_HOOK_LABEL_PREFIX) for hook_name in failed_teardown_hook_names)


def _determine_failed_stage(test_result_payload) -> str:
    failed_setup_hook_names = _collect_failed_stage_names(test_result_payload.get("beforeStages"))
    failed_teardown_hook_names = _collect_failed_stage_names(test_result_payload.get("afterStages"))
    test_body_stage = test_result_payload.get("testStage")
    body_failure_detected = _has_failed_like_status(test_body_stage.get("status") if isinstance(test_body_stage, dict) else None)
    if failed_setup_hook_names:
        return "setup"
    if failed_teardown_hook_names:
        return "teardown"
    if body_failure_detected and not _is_setup_failure_reflected_in_test_stage(test_body_stage):
        return "body"
    return ""


def _map_result_status(allure_status_text) -> str:
    if allure_status_text == "passed":
        return "pass"
    if allure_status_text == "skipped":
        return "skip"
    return "fail"


def _read_failure_timestamp_utc(test_result_payload) -> str:
    if _map_result_status(test_result_payload.get("status")) != "fail":
        return ""
    failed_phase_name = _determine_failed_stage(test_result_payload) or "body"
    failure_stop_timestamp_ms: int | None = None
    if failed_phase_name == "setup":
        failure_stop_timestamp_ms = _read_failed_stop_ms_from_stages(test_result_payload.get("beforeStages"))
    elif failed_phase_name == "teardown":
        failure_stop_timestamp_ms = _read_failed_stop_ms_from_stages(test_result_payload.get("afterStages"))
    elif failed_phase_name == "body":
        failure_stop_timestamp_ms = _read_failed_stop_ms_from_stage_tree(test_result_payload.get("testStage"))
    if failure_stop_timestamp_ms is None:
        failure_stop_timestamp_ms = _read_stop_ms(test_result_payload.get("time"))
    return _format_utc_timestamp_from_ms(failure_stop_timestamp_ms)


def _convert_ms_to_seconds(milliseconds_value) -> int:
    return milliseconds_value // 1000


def _gather_step_times(stage_payload) -> list[dict]:
    if not isinstance(stage_payload, dict):
        return []
    nested_step_times: list[dict] = []
    for step_entry in stage_payload.get("steps") or []:
        if not isinstance(step_entry, dict):
            continue
        step_time_payload = step_entry.get("time")
        if isinstance(step_time_payload, dict):
            nested_step_times.append(step_time_payload)
        nested_step_times.extend(_gather_step_times(step_entry))
    return nested_step_times


def _read_duration_ms(time_payload: object) -> int:
    if not isinstance(time_payload, dict):
        return 0
    duration_value = time_payload.get("duration")
    if isinstance(duration_value, int) and duration_value >= 0:
        return duration_value
    start_timestamp = time_payload.get("start")
    stop_timestamp = time_payload.get("stop")
    if isinstance(start_timestamp, int) and isinstance(stop_timestamp, int) and stop_timestamp >= start_timestamp:
        return stop_timestamp - start_timestamp
    return 0


def _read_test_body_duration_ms(test_result_payload, total_duration_ms) -> int:
    test_body_stage = test_result_payload.get("testStage")
    if isinstance(test_body_stage, dict):
        stage_time_payload = test_body_stage.get("time")
        stage_duration_ms = _read_duration_ms(stage_time_payload) if isinstance(stage_time_payload, dict) else 0
        if stage_duration_ms > 0:
            return stage_duration_ms
        nested_step_times = _gather_step_times(test_body_stage)
        start_timestamps = [entry["start"] for entry in nested_step_times if isinstance(entry.get("start"), int)]
        stop_timestamps = [entry["stop"] for entry in nested_step_times if isinstance(entry.get("stop"), int)]
        if start_timestamps and stop_timestamps:
            return max(stop_timestamps) - min(start_timestamps)
    return total_duration_ms


def _sum_stage_duration_ms(stage_items) -> int:
    if not isinstance(stage_items, list):
        return 0
    total_duration = 0
    for stage_entry in stage_items:
        if not isinstance(stage_entry, dict):
            continue
        stage_time_payload = stage_entry.get("time")
        if isinstance(stage_time_payload, dict):
            total_duration += _read_duration_ms(stage_time_payload)
    return total_duration


def _find_test_id_in_labels(label_items, test_identifier_pattern: re.Pattern[str]) -> str:
    if not isinstance(label_items, list):
        return ""
    for label_entry in label_items:
        if not isinstance(label_entry, dict):
            continue
        label_text = label_entry.get("value")
        if not isinstance(label_text, str):
            continue
        matched_value = test_identifier_pattern.search(label_text)
        if matched_value:
            return matched_value.group(0)
    return ""


def parse_allure_report_archive(
    report_archive_bytes: bytes,
    test_identifier_prefix: str,
    test_identifier_marker: str,
) -> list[dict[str, str | int]]:
    test_identifier_pattern = re.compile(rf"{re.escape(test_identifier_prefix)}\d+")
    parsed_results: list[dict[str, str | int]] = []
    used_test_run_identifiers: set[str] = set()

    with ZipFile(io.BytesIO(report_archive_bytes)) as zip_archive:
        test_case_files = [
            entry_name for entry_name in zip_archive.namelist() if entry_name.endswith(".json") and "/data/test-cases/" in f"/{entry_name}"
        ]

        for test_case_file in sorted(test_case_files):
            raw_payload = zip_archive.read(test_case_file).decode("utf-8")
            test_case_payload = json.loads(raw_payload)
            full_test_name = test_case_payload.get("fullName")
            short_test_name = test_case_payload.get("name")
            source_file_path, test_case_name = _parse_test_identity(full_name_text=full_test_name, short_name_text=short_test_name)
            overall_duration_ms_value = _read_duration_ms(test_case_payload.get("time"))
            execution_stop_timestamp_ms = 0
            timing_payload = test_case_payload.get("time")
            if isinstance(timing_payload, dict):
                stop_candidate_ms = timing_payload.get("stop")
                if isinstance(stop_candidate_ms, int) and stop_candidate_ms >= 0:
                    execution_stop_timestamp_ms = stop_candidate_ms
            setup_duration_ms = _sum_stage_duration_ms(test_case_payload.get("beforeStages"))
            teardown_duration_ms = _sum_stage_duration_ms(test_case_payload.get("afterStages"))
            body_duration_ms = _read_test_body_duration_ms(test_result_payload=test_case_payload, total_duration_ms=overall_duration_ms_value)
            total_duration_seconds = _convert_ms_to_seconds(overall_duration_ms_value)
            setup_duration_seconds = _convert_ms_to_seconds(setup_duration_ms)
            body_duration_seconds = _convert_ms_to_seconds(body_duration_ms)
            teardown_duration_seconds = _convert_ms_to_seconds(teardown_duration_ms)
            normalized_result_status = _map_result_status(test_case_payload.get("status"))
            failed_phase_name = ""
            failure_timestamp_utc_text = ""
            if normalized_result_status == "fail":
                failed_phase_name = _determine_failed_stage(test_case_payload) or "body"
                failure_timestamp_utc_text = _read_failure_timestamp_utc(test_case_payload)
                if _has_teardown_publish_failure_only(test_case_payload):
                    normalized_result_status = "pass"
                    failure_timestamp_utc_text = ""
            test_run_identifier = _read_testrun_id(test_result_payload=test_case_payload, test_case_filename=test_case_file)
            unique_test_run_identifier = test_run_identifier
            duplicate_suffix_index = 2
            while unique_test_run_identifier in used_test_run_identifiers:
                unique_test_run_identifier = f"{test_run_identifier}-{duplicate_suffix_index}"
                duplicate_suffix_index += 1
            used_test_run_identifiers.add(unique_test_run_identifier)
            parsed_results.append(
                {
                    "file_path": source_file_path,
                    "test_name": test_case_name or test_case_file,
                    "status": normalized_result_status,
                    "testrun_id": unique_test_run_identifier,
                    "rerun_count": _read_rerun_count(test_case_payload),
                    "execution_stop_ms": execution_stop_timestamp_ms,
                    test_identifier_marker: _find_test_id_in_labels(test_case_payload.get("labels"), test_identifier_pattern),
                    "duration_total_s": total_duration_seconds,
                    "duration_setup_s": setup_duration_seconds,
                    "duration_body_s": body_duration_seconds,
                    "duration_teardown_s": teardown_duration_seconds,
                    "failed_stage": failed_phase_name,
                    "failure_timestamp_utc": failure_timestamp_utc_text,
                }
            )

    return parsed_results


def _standardize_test_id(test_id_text: str) -> tuple[str, str]:
    normalized_value = test_id_text.strip().upper()
    return normalized_value, normalized_value.replace("-", "_")


def _resolve_artifact_local_path(scope_directory: Path, artifact_record: ArtifactRecord) -> Path:
    return (scope_directory / artifact_record.relative_path).resolve()


def find_matching_test_logs_archive(run_record: RunRecord, scope_directory: Path, test_id_text: str) -> str:
    dash_identifier_variant, underscore_identifier_variant = _standardize_test_id(test_id_text)
    for artifact_record in run_record.artifacts:
        candidate_path = f"{artifact_record.source_relative_path}/{artifact_record.file_name}".upper()
        if artifact_record.artifact_type == "allure-report":
            continue
        if not candidate_path.endswith(".ZIP"):
            continue
        if dash_identifier_variant in candidate_path or underscore_identifier_variant in candidate_path:
            return str(_resolve_artifact_local_path(scope_directory, artifact_record))
    raise FileNotFoundError(
        f"No matching ZIP artifact was found for {test_id_text}. Expected a ZIP name containing {dash_identifier_variant} or {underscore_identifier_variant}"
    )


def attach_test_logs_to_single_result(
    single_job_result: dict[str, Any],
    run_record: RunRecord,
    scope_directory: Path,
    test_identifier_marker: str,
) -> None:
    normalized_run_url = str(single_job_result.get("job_url") or "")
    test_entries = single_job_result.get("tests") or []
    test_logs_artifact_cache: dict[tuple[str, str], str | None] = {}

    for test_entry in test_entries:
        test_identifier = str(test_entry.get(test_identifier_marker) or "").strip()
        if not test_identifier:
            test_entry["test_logs_archive_path"] = ""
            continue

        artifact_cache_key = (normalized_run_url, test_identifier.upper())
        if artifact_cache_key in test_logs_artifact_cache:
            test_entry["test_logs_archive_path"] = test_logs_artifact_cache[artifact_cache_key] or ""
            continue

        try:
            artifact_archive_path_text = find_matching_test_logs_archive(
                run_record=run_record,
                scope_directory=scope_directory,
                test_id_text=test_identifier,
            )
            test_logs_artifact_cache[artifact_cache_key] = artifact_archive_path_text
            test_entry["test_logs_archive_path"] = artifact_archive_path_text
        except FileNotFoundError as error_details:
            LOGGER.debug(
                "Could not find a test-logs artifact for job=%s, test_id=%s: %s",
                normalized_run_url,
                test_identifier,
                error_details,
            )
            test_logs_artifact_cache[artifact_cache_key] = None
            test_entry["test_logs_archive_path"] = ""
        except Exception as error_details:
            LOGGER.warning(
                "Error: Unable to resolve test logs for job=%s, test_id=%s: %s",
                normalized_run_url,
                test_identifier,
                error_details,
            )
            LOGGER.warning("Traceback: %s", traceback.format_exc())
            test_logs_artifact_cache[artifact_cache_key] = None
            test_entry["test_logs_archive_path"] = ""


def has_skipped_backtrace_tuple_shape(pytest_trace_text: str) -> bool:
    trace_text_payload = pytest_trace_text.strip()
    if not trace_text_payload:
        return False
    try:
        parsed_tuple_payload = ast.literal_eval(trace_text_payload)
    except (SyntaxError, ValueError):
        return False
    if not isinstance(parsed_tuple_payload, tuple) or len(parsed_tuple_payload) < 3:
        return False
    skip_reason_text = parsed_tuple_payload[2]
    return isinstance(skip_reason_text, str) and skip_reason_text.startswith("Skipped:")


def _find_backtrace_members_in_archive(zip_archive: ZipFile) -> list[str]:
    matching_members: list[str] = []
    for member_name in zip_archive.namelist():
        if member_name.endswith("/"):
            continue
        member_basename = PurePosixPath(member_name).name
        if BACKTRACE_LOG_BASENAME_PATTERN.fullmatch(member_basename):
            matching_members.append(member_name)
    return sorted(matching_members)


def _read_backtrace_from_test_logs_archive(test_logs_archive_path: str) -> tuple[str, str]:
    archive_path = Path(test_logs_archive_path).expanduser()
    if not archive_path.is_file():
        raise FileNotFoundError(f"Test-logs archive not found: {archive_path}")
    try:
        with ZipFile(archive_path) as zip_archive:
            matching_members = _find_backtrace_members_in_archive(zip_archive)
            if not matching_members:
                raise FileNotFoundError(
                    f"No archive member matched pattern {BACKTRACE_LOG_BASENAME_PATTERN.pattern!r} in {archive_path}"
                )
            selected_member_name = matching_members[-1]
            backtrace_payload = zip_archive.read(selected_member_name).decode("utf-8", errors="replace")
            return selected_member_name, backtrace_payload
    except BadZipFile as error_details:
        raise ValueError(f"Invalid ZIP archive: {archive_path}") from error_details


def compose_backtrace_filename(testrun_identifier: str, test_id_text: str = "") -> str:
    safe_test_identifier = truncate_component(make_safe_component(test_id_text, default_value=""), max_length=60)
    safe_test_run_identifier = truncate_component(
        make_safe_component(testrun_identifier, default_value="unknown_testrun"),
        max_length=160,
    )
    file_prefix = f"{safe_test_identifier}_" if safe_test_identifier else ""
    return f"{file_prefix}{safe_test_run_identifier}_backtrace.txt"


def store_test_backtrace_files(single_job_result, target_backtrace_directory: Path, test_identifier_marker: str) -> int:
    test_entries = single_job_result.get("tests") or []
    target_backtrace_directory.mkdir(parents=True, exist_ok=True)
    saved_file_count = 0

    for entry_index, test_entry in enumerate(test_entries, start=1):
        test_run_identifier = str(test_entry.get("testrun_id") or "").strip()
        if not test_run_identifier:
            test_run_identifier = f"legacy-{entry_index}"
            test_entry["testrun_id"] = test_run_identifier

        test_identifier = str(test_entry.get(test_identifier_marker) or "").strip()
        test_logs_archive_path = str(test_entry.get("test_logs_archive_path") or "").strip()
        base_filename = compose_backtrace_filename(testrun_identifier=test_run_identifier, test_id_text=test_identifier)
        test_entry["backtrace_sha256"] = ""
        test_entry["backtrace_file_path"] = ""

        if not test_logs_archive_path:
            continue

        try:
            _, backtrace_payload = _read_backtrace_from_test_logs_archive(test_logs_archive_path)
        except Exception as error_details:
            LOGGER.warning(
                "Error: Could not read device backtrace from %s for test_id=%s: %s",
                test_logs_archive_path,
                test_identifier or test_run_identifier,
                error_details,
            )
            LOGGER.warning("Traceback: %s", traceback.format_exc())
            continue

        output_result_path = target_backtrace_directory / base_filename
        duplicate_suffix_index = 2
        while output_result_path.exists():
            output_result_path = target_backtrace_directory / f"{output_result_path.stem}-{duplicate_suffix_index}{output_result_path.suffix}"
            duplicate_suffix_index += 1

        try:
            output_result_path.write_text(backtrace_payload, encoding="utf-8")
            test_entry["backtrace_file_path"] = str(output_result_path)
            if output_result_path.stat().st_size > 0:
                payload_bytes = output_result_path.read_bytes()
                test_entry["backtrace_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
            saved_file_count += 1
        except Exception as error_details:
            LOGGER.warning("Error: Could not save backtrace file %s: %s", output_result_path, error_details)
            LOGGER.warning("Traceback: %s", traceback.format_exc())

    return saved_file_count


def remove_skipped_and_empty_backtraces(single_job_result) -> int:
    test_entries = single_job_result.get("tests") or []
    deleted_file_count = 0
    for test_entry in test_entries:
        backtrace_path = str(test_entry.get("backtrace_file_path") or "").strip()
        if not backtrace_path:
            continue
        backtrace_file = Path(backtrace_path)
        try:
            if not backtrace_file.exists():
                test_entry["backtrace_file_path"] = ""
                test_entry["backtrace_sha256"] = ""
                continue
            cleanup_reason = ""
            if backtrace_file.stat().st_size == 0:
                cleanup_reason = "empty"
            else:
                payload_text = backtrace_file.read_text(encoding="utf-8")
                if has_skipped_backtrace_tuple_shape(payload_text):
                    cleanup_reason = "skipped"
            if not cleanup_reason:
                continue
            backtrace_file.unlink(missing_ok=True)
            test_entry["backtrace_file_path"] = ""
            test_entry["backtrace_sha256"] = ""
            deleted_file_count += 1
        except Exception as error_details:
            LOGGER.warning("Error: Could not clean up backtrace file %s: %s", backtrace_file, error_details)
            LOGGER.warning("Traceback: %s", traceback.format_exc())
    return deleted_file_count


def _coerce_execution_stop_ms(stop_ms_value) -> int:
    try:
        return int(str(stop_ms_value).strip())
    except (TypeError, ValueError):
        return -1


def _coerce_rerun_count(rerun_value) -> int:
    try:
        return int(str(rerun_value).strip())
    except (TypeError, ValueError):
        return -1


def keep_latest_test_executions_per_job(csv_records: list[dict[str, str]]) -> list[dict[str, str]]:
    latest_record_by_key: dict[tuple[str, str, str, str], tuple[int, int, int, dict[str, str]]] = {}
    for record_index, result_record in enumerate(csv_records):
        lookup_key = (
            result_record.get("job_name", ""),
            result_record.get("job_run_number", ""),
            result_record.get("file_path", ""),
            result_record.get("test_name", ""),
        )
        rerun_attempt_count = _coerce_rerun_count(result_record.get("rerun_count", ""))
        execution_stop_timestamp_ms = _coerce_execution_stop_ms(result_record.get("execution_stop_ms", ""))
        saved_record = latest_record_by_key.get(lookup_key)
        if (
            saved_record is None
            or rerun_attempt_count > saved_record[0]
            or (rerun_attempt_count == saved_record[0] and execution_stop_timestamp_ms > saved_record[1])
            or (rerun_attempt_count == saved_record[0] and execution_stop_timestamp_ms == saved_record[1] and record_index > saved_record[2])
        ):
            latest_record_by_key[lookup_key] = (rerun_attempt_count, execution_stop_timestamp_ms, record_index, result_record)
    return [entry[3] for entry in sorted(latest_record_by_key.values(), key=lambda entry: entry[2])]


def _stringify_csv_value(field_value) -> str:
    if field_value is None:
        return ""
    return str(field_value)


def expand_single_result_to_csv_rows(single_job_result, test_identifier_marker: str) -> list[dict[str, str]]:
    job_display_name = str(single_job_result.get("job_name") or "")
    job_run_number = str(single_job_result.get("job_run_number") or "")
    result_records: list[dict[str, str]] = []
    for test_entry in single_job_result.get("tests") or []:
        result_records.append(
            {
                "job_name": job_display_name,
                "job_run_number": job_run_number,
                "file_path": str(test_entry.get("file_path") or ""),
                "test_name": str(test_entry.get("test_name") or ""),
                "testrun_id": str(test_entry.get("testrun_id") or ""),
                "rerun_count": _stringify_csv_value(test_entry.get("rerun_count")),
                "execution_stop_ms": _stringify_csv_value(test_entry.get("execution_stop_ms")),
                "result": str(test_entry.get("status") or "fail"),
                test_identifier_marker: str(test_entry.get(test_identifier_marker) or ""),
                "duration_total_s": _stringify_csv_value(test_entry.get("duration_total_s")),
                "duration_setup_s": _stringify_csv_value(test_entry.get("duration_setup_s")),
                "duration_body_s": _stringify_csv_value(test_entry.get("duration_body_s")),
                "duration_teardown_s": _stringify_csv_value(test_entry.get("duration_teardown_s")),
                "failed_stage": str(test_entry.get("failed_stage") or ""),
                "failure_timestamp_utc": str(test_entry.get("failure_timestamp_utc") or ""),
                "test_logs_archive_path": str(test_entry.get("test_logs_archive_path") or ""),
            }
        )
    return result_records


def write_results_to_csv(csv_records: list[dict[str, str]], destination_csv_path: Path, test_identifier_marker: str) -> None:
    csv_headers = [
        "job_name",
        "job_run_number",
        "file_path",
        "test_name",
        "testrun_id",
        "rerun_count",
        "result",
        test_identifier_marker,
        "duration_total_s",
        "duration_setup_s",
        "duration_body_s",
        "duration_teardown_s",
        "failed_stage",
        "failure_timestamp_utc",
        "test_logs_archive_path",
    ]
    destination_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with destination_csv_path.open("w", encoding="utf-8", newline="") as csv_handle:
        csv_writer = csv.DictWriter(csv_handle, fieldnames=csv_headers, delimiter=";", extrasaction="ignore")
        csv_writer.writeheader()
        csv_writer.writerows(csv_records)


def _handle_partial_mode(config: ReporterConfig, message: str, error_details: Exception | None = None) -> None:
    if error_details is not None:
        LOGGER.warning("%s: %s", message, error_details)
        LOGGER.warning("Traceback: %s", traceback.format_exc())
    else:
        LOGGER.warning("%s", message)
    if config.parsing.partial_mode == "fail":
        raise RuntimeError(message) from error_details


def _find_allure_artifact(run_record: RunRecord) -> ArtifactRecord | None:
    for artifact_record in run_record.artifacts:
        if artifact_record.artifact_type == "allure-report":
            return artifact_record
    for artifact_record in run_record.artifacts:
        candidate_path = f"{artifact_record.source_relative_path}/{artifact_record.file_name}".lower()
        if candidate_path.endswith("allure-report.zip"):
            return artifact_record
    return None


def select_output_csv_path(config: ReporterConfig, scope_directory: Path, manifest: Manifest) -> Path:
    if config.output.csv_path:
        return Path(config.output.csv_path).expanduser().resolve()
    return scope_directory / f"{make_safe_component(manifest.scope_name, default_value='report')}.csv"


def select_backtrace_directory(config: ReporterConfig, scope_directory: Path) -> Path:
    if config.output.backtrace_output_directory:
        return Path(config.output.backtrace_output_directory).expanduser().resolve()
    return scope_directory / "backtraces"


def generate_report(config: ReporterConfig) -> Path:
    validate_config(config)
    manifest, scope_directory = load_manifest(config)
    destination_csv_path = select_output_csv_path(config, scope_directory=scope_directory, manifest=manifest)
    backtrace_directory = select_backtrace_directory(config, scope_directory=scope_directory)

    collected_records: list[dict[str, str]] = []
    for run_record in manifest.runs:
        allure_artifact = _find_allure_artifact(run_record)
        if allure_artifact is None:
            _handle_partial_mode(config, f"Allure artifact not found for run {run_record.run_url}")
            continue

        allure_archive_path = _resolve_artifact_local_path(scope_directory, allure_artifact)
        if not allure_archive_path.is_file():
            _handle_partial_mode(config, f"Allure ZIP does not exist: {allure_archive_path}")
            continue

        try:
            parsed_results = parse_allure_report_archive(
                report_archive_bytes=allure_archive_path.read_bytes(),
                test_identifier_prefix=config.parsing.test_identifier_prefix,
                test_identifier_marker=config.parsing.test_identifier_marker,
            )
        except Exception as error_details:
            _handle_partial_mode(config, f"Unable to parse allure ZIP for run {run_record.run_url}", error_details)
            continue

        single_job_result = {
            "job_url": run_record.run_url,
            "job_name": run_record.job_name,
            "job_run_number": run_record.job_run_number,
            "tests": parsed_results,
            "tests_total": len(parsed_results),
        }
        attach_test_logs_to_single_result(
            single_job_result=single_job_result,
            run_record=run_record,
            scope_directory=scope_directory,
            test_identifier_marker=config.parsing.test_identifier_marker,
        )
        store_test_backtrace_files(
            single_job_result=single_job_result,
            target_backtrace_directory=backtrace_directory,
            test_identifier_marker=config.parsing.test_identifier_marker,
        )
        remove_skipped_and_empty_backtraces(single_job_result)
        collected_records.extend(expand_single_result_to_csv_rows(single_job_result, test_identifier_marker=config.parsing.test_identifier_marker))

    if config.parsing.retain_latest_execution_per_job:
        collected_records = keep_latest_test_executions_per_job(collected_records)

    write_results_to_csv(collected_records, destination_csv_path=destination_csv_path, test_identifier_marker=config.parsing.test_identifier_marker)
    LOGGER.info("CSV report written: %s", destination_csv_path)
    return destination_csv_path


def parse_args() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description="Generate CSV from a local downloader manifest and ZIP artifacts")
    parser.add_argument("--config", required=False, help="Path to reporter JSON config", default="config.json")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging(verbose=args.verbose)
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    csv_path = generate_report(config)
    LOGGER.info("Report complete. CSV: %s", csv_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
