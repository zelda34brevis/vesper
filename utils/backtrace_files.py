from __future__ import annotations

import ast
import hashlib
import logging
import re
import traceback
from pathlib import Path
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from .filesystem_helpers import make_safe_component, truncate_component
from settings import TEST_IDENTIFIER_MARKER

module_logger = logging.getLogger(__name__)
BACKTRACE_LOG_BASENAME_PATTERN = re.compile(r"^backtrace_.*\.log$", re.IGNORECASE)


def has_skipped_backtrace_tuple_shape(pytest_trace_text) -> bool:
    """Return True when the backtrace is tuple-like and the third item contains a Skipped reason."""
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


def remove_skipped_and_empty_backtraces(single_job_result) -> int:
    """Remove backtrace files for skipped tests or empty traces and clear the related metadata."""
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
                payload_bytes = backtrace_file.read_text(encoding="utf-8")
                if has_skipped_backtrace_tuple_shape(payload_bytes):
                    cleanup_reason = "skipped"

            if not cleanup_reason:
                continue

            backtrace_file.unlink(missing_ok=True)
            test_entry["backtrace_file_path"] = ""
            test_entry["backtrace_sha256"] = ""
            deleted_file_count += 1
            module_logger.debug(f"Removed backtrace file {backtrace_file} (reason={cleanup_reason})")
        except Exception as error_details:
            module_logger.warning(f"Error: Could not clean up backtrace file {backtrace_file}: {error_details}")
            module_logger.warning(f"Stack trace: {traceback.format_exc()}")

    return deleted_file_count

def _make_unique_output_path(target_backtrace_directory, candidate_filename) -> Path:
    output_result_path = target_backtrace_directory / candidate_filename
    duplicate_suffix_index = 2
    while output_result_path.exists():
        candidate_filename = f"{output_result_path.stem}-{duplicate_suffix_index}{output_result_path.suffix}"
        output_result_path = target_backtrace_directory / candidate_filename
        duplicate_suffix_index += 1
    return output_result_path


def _truncate_filename_component(filename_component, max_component_length) -> str:
    """Shorten long filename components while preserving deterministic uniqueness."""
    return truncate_component(component_text=filename_component, max_length=max_component_length)


def _make_safe_filename_component(raw_value, default_value) -> str:
    """Turn arbitrary text into a filesystem-safe ASCII-like component."""
    return make_safe_component(raw_value=raw_value, default_value=default_value, allow_dots=True)


def _find_backtrace_members_in_archive(zip_archive) -> list[str]:
    """Return sorted member names whose basename matches ``backtrace_.*.log``."""
    matching_members: list[str] = []
    for member_name in zip_archive.namelist():
        if member_name.endswith("/"):
            continue

        member_basename = PurePosixPath(member_name).name
        if BACKTRACE_LOG_BASENAME_PATTERN.fullmatch(member_basename):
            matching_members.append(member_name)

    return sorted(matching_members)


def _read_backtrace_from_test_logs_archive(test_logs_archive_path) -> tuple[str, str]:
    """Read the selected backtrace text from one downloaded per-test logs ZIP archive."""
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


def compose_backtrace_filename(testrun_identifier, test_id_text = "") -> str:
    """Compose a per-test backtrace filename as <test_id>_<testrun_id>_backtrace.txt."""
    safe_test_identifier = _truncate_filename_component(
        _make_safe_filename_component(test_id_text, default_value=""),
        max_component_length=60,
    )
    safe_test_run_identifier = _truncate_filename_component(
        _make_safe_filename_component(testrun_identifier, default_value="unknown_testrun"),
        max_component_length=160,
    )
    file_prefix = f"{safe_test_identifier}_" if safe_test_identifier else ""
    return f"{file_prefix}{safe_test_run_identifier}_backtrace.txt"


def store_test_backtrace_files(single_job_result, target_backtrace_directory) -> int:
    """Write one extracted device-backtrace text file per test into the scoped output directory."""
    test_entries = single_job_result.get("tests") or []
    target_backtrace_directory.mkdir(parents=True, exist_ok=True)
    saved_file_count = 0

    for entry_index, test_entry in enumerate(test_entries, start=1):
        test_run_identifier = str(test_entry.get("testrun_id") or "").strip()
        if not test_run_identifier:
            test_run_identifier = f"legacy-{entry_index}"
            test_entry["testrun_id"] = test_run_identifier

        test_identifier = str(test_entry.get(TEST_IDENTIFIER_MARKER) or "").strip()
        test_logs_archive_path = str(test_entry.get("test_logs_archive_path") or "").strip()
        base_filename = compose_backtrace_filename(testrun_identifier=test_run_identifier, test_id_text=test_identifier)
        test_entry["backtrace_sha256"] = ""
        test_entry["backtrace_file_path"] = ""

        if not test_logs_archive_path:
            module_logger.warning(
                f"Skipping backtrace extraction because test_logs_archive_path is empty for test_id={test_identifier or test_run_identifier}"
            )
            continue

        try:
            selected_archive_member_name, backtrace_payload = _read_backtrace_from_test_logs_archive(test_logs_archive_path)
        except Exception as error_details:
            module_logger.warning(
                f"Error: Could not read device backtrace from {test_logs_archive_path} for test_id={test_identifier or test_run_identifier}: {error_details}"
            )
            module_logger.warning(f"Stack trace: {traceback.format_exc()}")
            continue

        output_result_path = _make_unique_output_path(
            target_backtrace_directory=target_backtrace_directory,
            candidate_filename=base_filename,
        )

        try:
            output_result_path.write_text(backtrace_payload, encoding="utf-8")
            test_entry["backtrace_file_path"] = str(output_result_path)
            if output_result_path.stat().st_size > 0:
                payload_bytes = output_result_path.read_bytes()
                test_entry["backtrace_sha256"] = hashlib.sha256(payload_bytes).hexdigest()
            saved_file_count += 1
            module_logger.debug(
                f"Saved backtrace file for test_id={test_identifier or test_run_identifier}: archive={test_logs_archive_path}, member={selected_archive_member_name}, output={output_result_path}"
            )
        except Exception as error_details:
            module_logger.warning(f"Error: Could not save backtrace file {output_result_path}: {error_details}")
            module_logger.warning(f"Stack trace: {traceback.format_exc()}")

    return saved_file_count
