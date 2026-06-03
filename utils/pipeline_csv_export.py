from __future__ import annotations

import logging

from runtime_context import backtrace_output_directory
from .backtrace_files import remove_skipped_and_empty_backtraces, store_test_backtrace_files
from .downstream_jobruns import discover_downstream_jobrun_urls
from .jenkins_helpers import normalize_run_url, parse_job_name_and_run_number
from .multi_jobrun_csv_export import collect_from_jobruns_and_write_csv
from .pipeline_report_paths import make_pipeline_cache_scope_dirname
from .single_jobrun_results import collect_test_results_from_allure_report
from .test_result_enrichment import attach_test_logs_to_single_result


def _resolve_cache_scope_and_jobrun_urls(
        pipeline_execution_url: str | None,
        destination_csv_path,
        max_traversal_depth,
        jobrun_url_list: list[str] | None,
        scoped_logger) -> tuple[str, list[str]]:
    """Resolve the cache scope and the concrete list of job-run URLs to process."""
    if pipeline_execution_url is not None and jobrun_url_list is None:
        cache_scope_directory_name = make_pipeline_cache_scope_dirname(pipeline_execution_url)
        jobrun_urls = discover_downstream_jobrun_urls(
            pipeline_execution_url=pipeline_execution_url,
            max_traversal_depth=max_traversal_depth,
        )
        if jobrun_urls:
            return cache_scope_directory_name, jobrun_urls

        fallback_root_run_url = normalize_run_url(pipeline_execution_url)
        scoped_logger.warning(
            "No downstream job runs were found for root run %s. "
            "Switching to single-job mode and processing the root run directly.",
            fallback_root_run_url,
        )
        return cache_scope_directory_name, [fallback_root_run_url]

    if pipeline_execution_url is None and jobrun_url_list is not None:
        return destination_csv_path.stem, list(jobrun_url_list)

    raise ValueError("Invalid input: Provide either pipeline_execution_url OR job_list")


def export_multi_jobs_results(
        pipeline_execution_url: str | None,
        destination_csv_path,
        max_traversal_depth=25,
        jobrun_url_list: list[str] | None = None,
        retain_latest_execution_per_job=True) -> int:
    """Collect multi job execution, download artifacts and export all test results to single CSV."""

    module_logger = logging.getLogger(__name__)
    scoped_logger = module_logger
    cache_scope_directory_name, jobrun_urls = _resolve_cache_scope_and_jobrun_urls(
        pipeline_execution_url=pipeline_execution_url,
        destination_csv_path=destination_csv_path,
        max_traversal_depth=max_traversal_depth,
        jobrun_url_list=jobrun_url_list,
        scoped_logger=scoped_logger,
    )

    backtrace_scope_directory = (backtrace_output_directory / cache_scope_directory_name).resolve()
    scoped_logger.debug(f"Selected cache-scope directory: {cache_scope_directory_name}")
    scoped_logger.debug(f"Selected backtrace-scope directory: {backtrace_scope_directory}")

    scoped_logger.debug(f"Total job runs found: {len(jobrun_urls)}")

    successful_jobrun_urls: list[str] = []
    single_run_results: list[dict] = []
    test_logs_artifact_cache: dict[tuple[str, str], str | None] = {}
    failed_jobrun_count = 0

    for current_jobrun_url in jobrun_urls:
        job_display_name, job_run_number = parse_job_name_and_run_number(current_jobrun_url)
        scoped_logger.info(f"Processing downstream job started: {job_display_name} #{job_run_number}")
        try:
            single_run_result = collect_test_results_from_allure_report(
                current_jobrun_url,
                report_cache_scope_name=cache_scope_directory_name,
            )
            attach_test_logs_to_single_result(
                single_run_result,
                test_logs_artifact_cache=test_logs_artifact_cache,
                logs_scope_name=cache_scope_directory_name,
            )
            saved_backtrace_count = store_test_backtrace_files(
                single_run_result,
                target_backtrace_directory=backtrace_scope_directory,
            )
            deleted_backtrace_count = remove_skipped_and_empty_backtraces(single_run_result)
            scoped_logger.debug(
                f"Wrote per-test backtrace files for {job_display_name} #{job_run_number}: {saved_backtrace_count}"
            )
            scoped_logger.debug(
                f"Removed skipped or empty backtrace files for {job_display_name} #{job_run_number}: {deleted_backtrace_count}"
            )
            total_test_count = single_run_result.get("tests_total", 0)
            scoped_logger.debug(f"Validated run {current_jobrun_url}: total_tests={total_test_count}")
            successful_jobrun_urls.append(current_jobrun_url)
            single_run_results.append(single_run_result)
        except Exception as error_details:
            failed_jobrun_count += 1
            scoped_logger.warning(f"Problem: Could not complete single-run extraction for {current_jobrun_url}: {error_details}")

    scoped_logger.debug(
        f"Single-run extraction complete: success={len(successful_jobrun_urls)}, failed={failed_jobrun_count}"
    )

    record_count = collect_from_jobruns_and_write_csv(
        requested_job_run_urls=successful_jobrun_urls,
        destination_csv_path=destination_csv_path,
        preloaded_single_run_results=single_run_results,
        retain_latest_execution_per_job=retain_latest_execution_per_job,
    )
    scoped_logger.debug(f"Pipeline export complete: rows_count={record_count}, output_csv={destination_csv_path}")
    return record_count
