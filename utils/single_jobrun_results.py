from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import quote

from runtime_context import JENKINS_USERNAME, JENKINS_API_TOKEN
from settings import ALLURE_REPORTS_DIRECTORY
from . import allure_zip_parser
from .filesystem_helpers import make_safe_component
from .jenkins_helpers import canonicalize_run_url, create_url_opener, fetch_url_bytes, fetch_url_json, \
    parse_job_name_and_run_number

module_logger = logging.getLogger(__name__)
allure_reports_directory = Path(ALLURE_REPORTS_DIRECTORY).expanduser()


def collect_test_results_from_allure_report(
    job_run_url,
    jenkins_username = None,
    jenkins_api_token = None,
    request_timeout_seconds = 20,
    report_cache_scope_name = None,
) -> dict[str, Any]:
    """Return one job-run result as a dictionary containing parsed test statuses."""
    module_logger.info(f"Starting single job-run extraction for: {job_run_url}")
    jenkins_username = JENKINS_USERNAME if jenkins_username is None else jenkins_username
    jenkins_api_token = JENKINS_API_TOKEN if jenkins_api_token is None else jenkins_api_token

    module_logger.debug(f"Normalizing run URL: {job_run_url}")
    normalized_run_url = canonicalize_run_url(job_run_url)
    module_logger.debug(f"Normalized run URL: {normalized_run_url}")
    module_logger.debug(f"Creating opener: auth_enabled={bool(jenkins_username and jenkins_api_token)}")
    http_opener = create_url_opener(jenkins_username=jenkins_username, jenkins_api_token=jenkins_api_token)
    if http_opener is None:
        module_logger.info("Continuing without Jenkins basic auth (username/token not provided)")
    else:
        module_logger.debug("Created HTTP opener with basic auth")
    module_logger.debug(f"Reading job name and run number from URL: {normalized_run_url}")
    job_display_name, job_run_number = parse_job_name_and_run_number(normalized_run_url)
    module_logger.debug(f"Read job_name={job_display_name}, run_number={job_run_number}")
    module_logger.debug(f"Using normalized run URL: {normalized_run_url}")

    try:
        api_tree = "artifacts[fileName,relativePath]"
        api_endpoint_url = f"{normalized_run_url}api/json?tree={api_tree}"
        module_logger.info(f"Fetching JSON URL: {api_endpoint_url} (timeout={request_timeout_seconds}s)")
        build_metadata = fetch_url_json(api_endpoint_url, url_opener=http_opener, request_timeout_seconds=request_timeout_seconds)
        module_logger.debug(f"Successfully parsed JSON payload from {api_endpoint_url}")
    except Exception as error_details:
        module_logger.warning(f"Error: Unable to read Jenkins artifacts for {normalized_run_url}: {error_details}")
        module_logger.warning(f"Stack trace: {traceback.format_exc()}")
        raise RuntimeError(f"Unable to fetch Jenkins artifacts for {normalized_run_url}") from error_details

    artifact_entries = build_metadata.get("artifacts") or []
    module_logger.info(f"Artifacts found in build metadata: {len(artifact_entries)}")
    allure_archive_artifact = None
    for artifact_entry in artifact_entries:
        artifact_filename = artifact_entry.get("fileName", "")
        artifact_relative_path = artifact_entry.get("relativePath", "")
        if artifact_filename == "allure-report.zip" or artifact_relative_path.endswith("allure-report.zip"):
            allure_archive_artifact = artifact_entry
            break

    if not allure_archive_artifact:
        module_logger.warning(f"Error: The allure-report.zip artifact was not found for {normalized_run_url}")
        raise RuntimeError(f"The allure-report.zip artifact was not found for {normalized_run_url}")

    artifact_relative_path = allure_archive_artifact["relativePath"]
    module_logger.info(f"Found allure artifact at path: {artifact_relative_path}")
    quoted_artifact_path = quote(artifact_relative_path, safe="/")
    artifact_download_url = f"{normalized_run_url}artifact/{quoted_artifact_path}"

    safe_job_label = make_safe_component(job_display_name or "", default_value="unknown_job", allow_dots=True)
    safe_run_label = make_safe_component(job_run_number or "", default_value="unknown_run", allow_dots=True)
    allure_reports_root_directory = allure_reports_directory
    if report_cache_scope_name:
        allure_reports_root_directory = allure_reports_root_directory / make_safe_component(
            report_cache_scope_name,
            default_value="unknown_scope",
            allow_dots=True,
        )
    allure_archive_path = allure_reports_root_directory / f"{safe_job_label}-{safe_run_label}-allure-report.zip"
    if allure_archive_path.exists():
        module_logger.info(f"Reusing allure ZIP: {allure_archive_path}")
        try:
            zip_archive_bytes = allure_archive_path.read_bytes()
        except Exception as error_details:
            module_logger.warning(f"Error: Unable to read the allure report archive {allure_archive_path}: {error_details}")
            module_logger.warning(f"Stack trace: {traceback.format_exc()}")
            raise RuntimeError(f"Unable to read the allure report archive {allure_archive_path}") from error_details
    else:
        try:
            module_logger.info(f"Fetching bytes URL: {artifact_download_url} (timeout={request_timeout_seconds}s)")
            zip_archive_bytes = fetch_url_bytes(artifact_download_url, url_opener=http_opener, request_timeout_seconds=request_timeout_seconds)
            module_logger.debug(f"Fetched {len(zip_archive_bytes)} bytes from {artifact_download_url}")
        except Exception as error_details:
            module_logger.warning(f"Error: Unable to download {artifact_download_url}: {error_details}")
            module_logger.warning(f"Stack trace: {traceback.format_exc()}")
            raise RuntimeError(f"Unable to download the artifact from {artifact_download_url}") from error_details

        try:
            allure_archive_path.parent.mkdir(parents=True, exist_ok=True)
            allure_archive_path.write_bytes(zip_archive_bytes)
            module_logger.info(f"Saved allure ZIP: {allure_archive_path}")
        except Exception as error_details:
            module_logger.warning(f"Error: Unable to write the allure report archive {allure_archive_path}: {error_details}")
            module_logger.warning(f"Stack trace: {traceback.format_exc()}")

    try:
        parsed_results = allure_zip_parser.parse_allure_report_archive(zip_archive_bytes)
    except Exception as error_details:
        module_logger.warning(f"Error: Unable to parse allure-report.zip from {normalized_run_url}: {error_details}")
        module_logger.warning(f"Stack trace: {traceback.format_exc()}")
        raise RuntimeError(f"Unable to parse allure-report.zip for {normalized_run_url}") from error_details

    if not parsed_results:
        module_logger.warning(f"Error: No test cases were found in allure-report.zip for {normalized_run_url}")

    job_result_payload = {
        "job_url": normalized_run_url,
        "job_name": job_display_name,
        "job_run_number": job_run_number,
        "tests": parsed_results,
        "tests_total": len(parsed_results),
    }
    module_logger.info(
        f"Finished single job-run extraction: job_name={job_display_name}, run_number={job_run_number}, tests_total={len(parsed_results)}"
    )
    return job_result_payload


def _compose_allure_zip_path(
    job_name_text,
    job_run_id,
    report_cache_scope_name = None,
) -> Path:
    """Create the allure-report file path for a downloaded allure-report.zip artifact."""
    safe_job_label = make_safe_component(job_name_text or "", default_value="unknown_job", allow_dots=True)
    safe_run_label = make_safe_component(job_run_id or "", default_value="unknown_run", allow_dots=True)
    allure_reports_root_directory = allure_reports_directory
    if report_cache_scope_name:
        allure_reports_root_directory = allure_reports_root_directory / make_safe_component(report_cache_scope_name, default_value="unknown_scope", allow_dots=True)

    return allure_reports_root_directory / f"{safe_job_label}-{safe_run_label}-allure-report.zip"

