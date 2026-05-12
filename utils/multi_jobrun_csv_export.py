from __future__ import annotations

import csv
import logging
import traceback
from pathlib import Path

from .single_jobrun_results import collect_test_results_from_allure_report
from settings import TEST_IDENTIFIER_MARKER

module_logger = logging.getLogger(__name__)


def write_results_to_csv(csv_records, destination_csv_path) -> None:
    """Write aggregated test results into a CSV file that uses ';' as the delimiter."""
    module_logger.info(f"Writing CSV file: {destination_csv_path} (rows={len(csv_records)})")
    csv_headers = [
        "job_name",
        "job_run_number",
        "file_path",
        "test_name",
        "testrun_id",
        "rerun_count",
        "result",
        TEST_IDENTIFIER_MARKER,
        "duration_total_s",
        "duration_setup_s",
        "duration_body_s",
        "duration_teardown_s",
        "failed_stage",
        "failure_timestamp_utc",
        "test_logs_archive_path",
    ]
    with destination_csv_path.open("w", encoding="utf-8", newline="") as csv_handle:
        csv_writer = csv.DictWriter(csv_handle, fieldnames=csv_headers, delimiter=";", extrasaction="ignore")
        csv_writer.writeheader()
        csv_writer.writerows(csv_records)
    module_logger.info(f"Finished writing CSV file: {destination_csv_path}")


def _coerce_execution_stop_ms(stop_ms_value) -> int:
    """Convert the execution stop timestamp (ms) for tie-breaking, defaulting to -1."""
    try:
        return int(str(stop_ms_value).strip())
    except (TypeError, ValueError):
        return -1


def _coerce_rerun_count(rerun_value) -> int:
    """Convert a rerun_count string to int, returning -1 for missing or invalid values."""
    try:
        return int(str(rerun_value).strip())
    except (TypeError, ValueError):
        return -1


def _keep_latest_test_executions_per_job(csv_records) -> list[dict[str, str]]:
    """Retain only the latest execution of each test within the same job run."""
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

    filtered_records = [entry[3] for entry in sorted(latest_record_by_key.values(), key=lambda entry: entry[2])]
    module_logger.info(f"Duplicate test executions filtered: before={len(csv_records)}, remaining={len(filtered_records)}")
    return filtered_records


def _stringify_csv_value(field_value) -> str:
    """Render an optional value as a CSV string while preserving numeric 0."""
    if field_value is None:
        return ""
    return str(field_value)


def _expand_single_result_to_csv_rows(single_job_result) -> list[dict[str, str]]:
    """Expand one single-run result dictionary into CSV rows."""
    job_display_name = str(single_job_result.get("job_name") or "")
    job_run_number = str(single_job_result.get("job_run_number") or "")
    module_logger.debug(f"Expanding single result into CSV rows: job_name={job_display_name}, run_number={job_run_number}")
    result_records: list[dict[str, str]] = []
    test_entries = single_job_result.get("tests") or []
    if not test_entries:
        module_logger.warning(
            f"No tests were found in the single result for job_name={job_display_name}, run_number={job_run_number}. "
            "The CSV will not contain rows for this run."
        )
    module_logger.debug(f"Single-result test count: {len(test_entries)}")

    for test_entry in test_entries:
        result_records.append(
            {
                "job_name": job_display_name,
                "job_run_number": job_run_number,
                "file_path": str(test_entry.get("file_path") or ""),
                "test_name": str(test_entry.get("test_name") or ""),
                "testrun_id": str(test_entry.get("testrun_id") or ""),
                "rerun_count": _stringify_csv_value(test_entry.get("rerun_count")),
                # Internal tie-break metadata consumed by the duplicate-filtering logic.
                "execution_stop_ms": _stringify_csv_value(test_entry.get("execution_stop_ms")),
                "result": str(test_entry.get("status") or "fail"),
                TEST_IDENTIFIER_MARKER: str(test_entry.get(TEST_IDENTIFIER_MARKER) or ""),
                "duration_total_s": _stringify_csv_value(test_entry.get("duration_total_s")),
                "duration_setup_s": _stringify_csv_value(test_entry.get("duration_setup_s")),
                "duration_body_s": _stringify_csv_value(test_entry.get("duration_body_s")),
                "duration_teardown_s": _stringify_csv_value(test_entry.get("duration_teardown_s")),
                "failed_stage": str(test_entry.get("failed_stage") or ""),
                "failure_timestamp_utc": str(test_entry.get("failure_timestamp_utc") or ""),
                "test_logs_archive_path": str(test_entry.get("test_logs_archive_path") or ""),
            }
        )

    module_logger.debug(f"Expanded row count: {len(result_records)}")
    return result_records


def collect_from_jobruns_and_write_csv_from_single_results(
    requested_job_run_urls,
    destination_csv_path,
    preloaded_single_run_results = None,
    retain_latest_execution_per_job = True,
) -> int:
    """Collect tests from all job runs (or reuse supplied single-run dict results) and save one CSV file."""
    module_logger.info("Gathering rows for CSV export")
    collected_records: list[dict[str, str]] = []

    if preloaded_single_run_results is not None:
        module_logger.info(f"Using preloaded single-run data: {len(preloaded_single_run_results)}")
        for single_run_result in preloaded_single_run_results:
            collected_records.extend(_expand_single_result_to_csv_rows(single_run_result))

        if retain_latest_execution_per_job:
            collected_records = _keep_latest_test_executions_per_job(collected_records)

        write_results_to_csv(collected_records, destination_csv_path=destination_csv_path)
        module_logger.info(f"Completed CSV export from preloaded results (rows={len(collected_records)})")
        return len(collected_records)

    module_logger.info(f"Fetching and expanding job URLs: {len(requested_job_run_urls)}")
    for job_base_url in requested_job_run_urls:
        module_logger.debug(f"Working on job URL: {job_base_url}")
        try:
            single_run_result = collect_test_results_from_allure_report(job_base_url)
        except Exception as error_details:
            module_logger.warning(f"Problem: Could not process {job_base_url}: {error_details}")
            module_logger.warning(f"Stack trace: {traceback.format_exc()}")
            continue

        collected_records.extend(_expand_single_result_to_csv_rows(single_run_result))

    if retain_latest_execution_per_job:
        collected_records = _keep_latest_test_executions_per_job(collected_records)

    write_results_to_csv(collected_records, destination_csv_path=destination_csv_path)
    module_logger.info(f"Completed CSV export (rows={len(collected_records)})")
    return len(collected_records)


def collect_from_jobruns_and_write_csv(
    requested_job_run_urls,
    destination_csv_path,
    preloaded_single_run_results = None,
    retain_latest_execution_per_job = True,
) -> int:
    """Collect tests from all job runs and save them into one CSV file."""
    module_logger.info(
        f"Launching multi-job CSV export: job_urls={len(requested_job_run_urls)}, output_path={destination_csv_path}, preloaded_results={preloaded_single_run_results is not None}"
    )
    return collect_from_jobruns_and_write_csv_from_single_results(
        requested_job_run_urls=requested_job_run_urls,
        destination_csv_path=destination_csv_path,
        preloaded_single_run_results=preloaded_single_run_results,
        retain_latest_execution_per_job=retain_latest_execution_per_job,
    )


if __name__ == "__main__":
    output_file_path = Path(__file__).resolve().parent / "allure_jobs_results.csv"

    demo_job_urls = [
    ]

    record_count = collect_from_jobruns_and_write_csv(demo_job_urls, destination_csv_path=output_file_path)
    print(f"Wrote {record_count} rows to {output_file_path}")
