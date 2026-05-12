from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from zipfile import ZipFile
from settings import TEST_IDENTIFIER_PREFIX, TEST_IDENTIFIER_MARKER

module_logger = logging.getLogger(__name__)
TEST_IDENTIFIER_PATTERN_TEXT = r"%%TEST_ID_PREFIX%%\d+"
TEST_IDENTIFIER_PATTERN_TEXT = TEST_IDENTIFIER_PATTERN_TEXT.replace('%%TEST_ID_PREFIX%%', TEST_IDENTIFIER_PREFIX)
TEST_IDENTIFIER_PATTERN = re.compile(TEST_IDENTIFIER_PATTERN_TEXT)

REPORT_TEARDOWN_HOOK_LABEL_PREFIX = "report_test_fixture"


def _coerce_non_negative_int(candidate_value) -> int | None:
    """Convert a payload value to a non-negative integer when possible."""
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
    """Extract the rerun count from an Allure parameters list."""
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
    """Extract the rerun count from an Allure payload across known schema variants."""
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
    """Normalize a testrun ID into a compact token suitable for filesystems and CSV."""
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
    """Extract a roughly unique ID for one test run from the Allure payload, falling back to the case-file stem."""
    for lookup_key in ("uid", "historyId", "testCaseId"):
        normalized_value = _sanitize_testrun_id(test_result_payload.get(lookup_key))
        if normalized_value:
            return normalized_value

    return _sanitize_testrun_id(PurePosixPath(test_case_filename).stem) or "unknown_testrun"


def _read_trace_from_status_fields(status_payload) -> str:
    """Read traceback-like text from common Allure status fields."""
    if not isinstance(status_payload, dict):
        return ""

    # Different Allure producers store traceback/message values under different keys.
    for lookup_key in ("statusTrace", "trace", "statusMessage", "message"):
        current_value = status_payload.get(lookup_key)
        if isinstance(current_value, str) and current_value.strip():
            return current_value.strip()

    status_details_payload = status_payload.get("statusDetails")
    if not isinstance(status_details_payload, dict):
        return ""

    for lookup_key in ("trace", "statusTrace", "message", "statusMessage"):
        current_value = status_details_payload.get(lookup_key)
        if isinstance(current_value, str) and current_value.strip():
            return current_value.strip()

    return ""


def _read_trace_from_stage_tree(stage_payload) -> str:
    """Recursively search the stage payload and nested steps for traceback text."""
    if not isinstance(stage_payload, dict):
        return ""

    direct_traceback = _read_trace_from_status_fields(stage_payload)
    if direct_traceback:
        return direct_traceback

    step_entries = stage_payload.get("steps")
    if not isinstance(step_entries, list):
        return ""

    for step_entry in step_entries:
        trace_text = _read_trace_from_stage_tree(step_entry)
        if trace_text:
            return trace_text

    return ""


def _read_pytest_trace(test_result_payload) -> str:
    """Read pytest traceback text from case-level and nested stage payloads."""
    test_body_stage = test_result_payload.get("testStage")
    if isinstance(test_body_stage, dict):
        trace_text = _read_trace_from_stage_tree(test_body_stage)
        if trace_text:
            return trace_text

    trace_text = _read_trace_from_status_fields(test_result_payload)
    if trace_text:
        return trace_text

    for stage_group_key in ("beforeStages", "afterStages"):
        stage_entries = test_result_payload.get(stage_group_key)
        if not isinstance(stage_entries, list):
            continue
        for stage_entry in stage_entries:
            if not isinstance(stage_entry, dict):
                continue
            trace_text = _read_trace_from_stage_tree(stage_entry)
            if trace_text:
                return trace_text

    return ""


def _parse_test_identity(full_name_text, short_name_text) -> tuple[str, str]:
    """Split an Allure test identity into file path and test name."""
    module_logger.debug(f"Separating test identity: full_name={full_name_text}, short_name={short_name_text}")
    if full_name_text and "#" in full_name_text:
        source_file_path, test_case_name = full_name_text.rsplit("#", 1)
        module_logger.debug(f"Separated by '#': file_path={source_file_path}, test_name={test_case_name}")
        return source_file_path, test_case_name

    if full_name_text:
        return full_name_text, short_name_text or full_name_text

    if short_name_text:
        return "", short_name_text

    return "", ""


def _format_utc_timestamp_from_ms(timestamp_ms) -> str:
    """Format Unix epoch milliseconds as a UTC timestamp string with millisecond precision."""
    if timestamp_ms is None:
        return ""

    formatted_timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    return formatted_timestamp[:-3]


def _read_stop_ms(time_payload: object) -> int | None:
    """Read a non-negative stop timestamp from an Allure time payload."""
    if not isinstance(time_payload, dict):
        return None

    return _coerce_non_negative_int(time_payload.get("stop"))


def _has_failed_like_status(result_status) -> bool:
    """Return True when an Allure status represents a failure-style outcome."""
    return isinstance(result_status, str) and result_status in {"failed", "broken"}


def _is_setup_failure_reflected_in_test_stage(body_stage_payload) -> bool:
    """Detect the pytest+allure case where setup fails and testStage is marked failed with no executed steps."""
    if not isinstance(body_stage_payload, dict):
        return False
    if not _has_failed_like_status(body_stage_payload.get("status")):
        return False

    step_count = body_stage_payload.get("stepsCount")
    step_entries = body_stage_payload.get("steps")
    steps_present = isinstance(step_entries, list) and len(step_entries) > 0
    return step_count == 0 and not steps_present


def _read_failed_stop_ms_from_stage_tree(stage_payload) -> int | None:
    """Return the stop timestamp of the deepest failed node in a stage tree."""
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
    """Return a failure stop timestamp from before/after stage arrays."""
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
    """Return the names of failed stages from before/after stage arrays."""
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
    """Detect a failure caused only by the report-publish teardown hook."""
    failed_setup_hook_names = _collect_failed_stage_names(test_result_payload.get("beforeStages"))
    if failed_setup_hook_names:
        return False

    failed_teardown_hook_names = _collect_failed_stage_names(test_result_payload.get("afterStages"))
    if not failed_teardown_hook_names:
        return False

    if not all(hook_name.startswith(REPORT_TEARDOWN_HOOK_LABEL_PREFIX) for hook_name in failed_teardown_hook_names):
        return False

    return True


def _determine_failed_stage(test_result_payload) -> str:
    """Return one failed phase for a test: setup, body, teardown, or an empty string."""
    failed_setup_hook_names = _collect_failed_stage_names(test_result_payload.get("beforeStages"))
    failed_teardown_hook_names = _collect_failed_stage_names(test_result_payload.get("afterStages"))
    test_body_stage = test_result_payload.get("testStage")
    body_failure_detected = _has_failed_like_status(test_body_stage.get("status") if isinstance(test_body_stage, dict) else None)

    # Keep the priority deterministic when multiple phases are marked as failed.
    if failed_setup_hook_names:
        return "setup"

    if failed_teardown_hook_names:
        return "teardown"

    if body_failure_detected and not _is_setup_failure_reflected_in_test_stage(test_body_stage):
        return "body"

    return ""


def _map_result_status(allure_status_text) -> str:
    module_logger.debug(f"Translating Allure status: {allure_status_text}")
    if allure_status_text == "passed":
        return "pass"
    if allure_status_text == "skipped":
        return "skip"
    return "fail"


def _read_failure_timestamp_utc(test_result_payload) -> str:
    """Extract the failure moment from the Allure payload as a UTC timestamp string."""
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
    """Convert milliseconds into whole seconds."""
    return milliseconds_value // 1000


def _gather_step_times(stage_payload) -> list[dict]:
    """Recursively collect nested step-time dictionaries from testStage."""
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
    """Return the duration in milliseconds from an Allure time payload.

    Prefer the explicit ``duration`` field and fall back to ``stop - start`` when it is missing.
    """
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
    """Read the test-body duration, preferring testStage.time.duration when available."""
    test_body_stage = test_result_payload.get("testStage")
    if isinstance(test_body_stage, dict):
        stage_time_payload = test_body_stage.get("time")
        stage_duration_ms = _read_duration_ms(stage_time_payload) if isinstance(stage_time_payload, dict) else 0
        if stage_duration_ms > 0:
            return stage_duration_ms

        # Certain Allure exports omit testStage.time, so derive the span from nested steps.
        nested_step_times = _gather_step_times(test_body_stage)
        start_timestamps = [entry["start"] for entry in nested_step_times if isinstance(entry.get("start"), int)]
        stop_timestamps = [entry["stop"] for entry in nested_step_times if isinstance(entry.get("stop"), int)]
        if start_timestamps and stop_timestamps:
            return max(stop_timestamps) - min(start_timestamps)

    return total_duration_ms


def _sum_stage_duration_ms(stage_items) -> int:
    """Add up stage durations in milliseconds for before/after stage arrays."""
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


def _find_test_id_in_labels(label_items) -> str:
    """Retrieve the first test ID from the Allure labels list."""
    if not isinstance(label_items, list):
        return ""

    for label_entry in label_items:
        if not isinstance(label_entry, dict):
            continue

        label_text = label_entry.get("value")
        if not isinstance(label_text, str):
            continue

        matched_value = TEST_IDENTIFIER_PATTERN.search(label_text)
        if matched_value:
            return matched_value.group(0)

    return ""


def parse_allure_report_archive(report_archive_bytes) -> list[dict[str, str | int]]:
    """Parse downloaded allure-report.zip bytes into normalized test-result entries."""
    module_logger.info(f"Reading test cases from ZIP ({len(report_archive_bytes)} bytes)")
    parsed_results: list[dict[str, str | int]] = []
    used_test_run_identifiers: set[str] = set()

    with ZipFile(io.BytesIO(report_archive_bytes)) as zip_archive:
        archive_member_names = zip_archive.namelist()
        module_logger.debug(f"Total ZIP entries: {len(archive_member_names)}")
        test_case_files = [
            entry_name for entry_name in archive_member_names
            if entry_name.endswith(".json") and "/data/test-cases/" in f"/{entry_name}"
        ]
        module_logger.info(f"Located {len(test_case_files)} test-case JSON files")

        for test_case_file in sorted(test_case_files):
            module_logger.debug(f"Reading test-case file: {test_case_file}")
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
            body_duration_ms = _read_test_body_duration_ms(
                test_result_payload=test_case_payload,
                total_duration_ms=overall_duration_ms_value,
            )
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
                # Business rule: report publication is external reporting and must not label
                # the functional test as failed when the test steps themselves succeeded.
                if _has_teardown_publish_failure_only(test_case_payload):
                    module_logger.info(
                        f"Switching fail->pass because of a report-publication teardown error: {test_case_name or test_case_file}"
                    )
                    normalized_result_status = "pass"
                    failure_timestamp_utc_text = ""
            pytest_traceback = _read_pytest_trace(test_case_payload)
            test_run_identifier = _read_testrun_id(test_result_payload=test_case_payload, test_case_filename=test_case_file)
            unique_test_run_identifier = test_run_identifier
            duplicate_suffix_index = 2
            while unique_test_run_identifier in used_test_run_identifiers:
                unique_test_run_identifier = f"{test_run_identifier}-{duplicate_suffix_index}"
                duplicate_suffix_index += 1

            used_test_run_identifiers.add(unique_test_run_identifier)
            if pytest_traceback:
                module_logger.info(f"Collected pytest trace for {test_case_name or test_case_file}: {len(pytest_traceback)} chars")
                module_logger.debug(f"Pytest trace content for {test_case_name or test_case_file}:\n{pytest_traceback}")

            parsed_results.append(
                {
                    "file_path": source_file_path,
                    "test_name": test_case_name or test_case_file,
                    "status": normalized_result_status,
                    "testrun_id": unique_test_run_identifier,
                    "rerun_count": _read_rerun_count(test_case_payload),
                    "execution_stop_ms": execution_stop_timestamp_ms,
                    TEST_IDENTIFIER_MARKER: _find_test_id_in_labels(test_case_payload.get("labels")),
                    "duration_total_s": total_duration_seconds,
                    "duration_setup_s": setup_duration_seconds,
                    "duration_body_s": body_duration_seconds,
                    "duration_teardown_s": teardown_duration_seconds,
                    "failed_stage": failed_phase_name,
                    "failure_timestamp_utc": failure_timestamp_utc_text,
                    "pytest_trace": pytest_traceback,
                }
            )

    module_logger.info(f"Read {len(parsed_results)} test results from ZIP")
    return parsed_results
