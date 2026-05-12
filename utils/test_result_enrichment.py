from __future__ import annotations

import logging
import traceback

from . import test_logs_download
from settings import TEST_IDENTIFIER_MARKER

module_logger = logging.getLogger(__name__)


def attach_test_logs_to_single_result(
    single_job_result,
    test_logs_artifact_cache,
    logs_scope_name = None,
) -> None:
    """Fill test entries in single_result with the downloaded test-log archive path."""
    normalized_run_url = str(single_job_result.get("job_url") or "")
    test_entries = single_job_result.get("tests") or []

    for test_entry in test_entries:
        test_identifier = str(test_entry.get(TEST_IDENTIFIER_MARKER) or "").strip()
        if not test_identifier:
            test_entry["test_logs_archive_path"] = ""
            continue

        artifact_cache_key = (normalized_run_url, test_identifier.upper())
        if artifact_cache_key in test_logs_artifact_cache:
            test_entry["test_logs_archive_path"] = test_logs_artifact_cache[artifact_cache_key] or ""
            continue

        try:
            artifact_archive_path = test_logs_download.fetch_test_logs_artifact_for_test(
                job_run_url=normalized_run_url,
                test_id_text=test_identifier,
                logs_scope_name=logs_scope_name,
            )
            artifact_archive_path_text = str(artifact_archive_path)
            test_logs_artifact_cache[artifact_cache_key] = artifact_archive_path_text
            test_entry["test_logs_archive_path"] = artifact_archive_path_text
        except FileNotFoundError as error_details:
            module_logger.debug(
                f"Could not find a test-logs artifact for job={normalized_run_url}, test_id={test_identifier}: {error_details}"
            )
            test_logs_artifact_cache[artifact_cache_key] = None
            test_entry["test_logs_archive_path"] = ""
        except Exception as error_details:
            module_logger.warning(
                f"Error: Unable to download test logs for job={normalized_run_url}, test_id={test_identifier}: {error_details}"
            )
            module_logger.warning(f"Stack trace: {traceback.format_exc()}")
            test_logs_artifact_cache[artifact_cache_key] = None
            test_entry["test_logs_archive_path"] = ""
