from __future__ import annotations

import logging
import traceback
from collections import deque
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from runtime_context import JENKINS_USERNAME, JENKINS_API_TOKEN
from .jenkins_helpers import normalize_run_url, compose_job_path, create_url_opener, fetch_url_json

module_logger = logging.getLogger(__name__)


def _create_job_fetcher(
    jenkins_username = None,
    jenkins_api_token = None,
    request_timeout_seconds = 20,
) -> Callable[[str], dict[str, Any]]:
    """Create a helper that fetches Jenkins job JSON from a job URL."""
    resolved_username = JENKINS_USERNAME if jenkins_username is None else jenkins_username
    resolved_api_token = JENKINS_API_TOKEN if jenkins_api_token is None else jenkins_api_token
    authentication_enabled = bool(resolved_username and resolved_api_token)
    module_logger.info(
        f"Preparing Jenkins job fetcher (timeout={request_timeout_seconds}s, auth_enabled={authentication_enabled})"
    )

    http_opener = create_url_opener(jenkins_username=resolved_username, jenkins_api_token=resolved_api_token)
    if http_opener is not None:
        module_logger.debug("Job fetcher is configured with a basic-auth opener")
    else:
        module_logger.info("Job fetcher is configured without an auth opener")

    def fetch_job_payload(normalized_job_url) -> dict[str, Any]:
        module_logger.info(f"Retrieving Jenkins job payload from {normalized_job_url}")
        api_tree = "url,fullName,name,lastCompletedBuild[url,number],lastBuild[url,number]"
        api_endpoint_url = f"{normalize_run_url(normalized_job_url)}api/json?tree={api_tree}"
        module_logger.debug(f"Jenkins job API endpoint URL: {api_endpoint_url}")
        parsed_job_payload = fetch_url_json(target_url=api_endpoint_url, url_opener=http_opener, request_timeout_seconds=request_timeout_seconds)
        module_logger.debug(f"Retrieved and parsed the Jenkins job payload from {normalized_job_url}")
        return parsed_job_payload

    return fetch_job_payload


def _create_build_fetcher(
    jenkins_username = None,
    jenkins_api_token = None,
    request_timeout_seconds = 20,
) -> Callable[[str], dict[str, Any]]:
    """Create a helper that fetches Jenkins build JSON from a run URL."""
    resolved_username = JENKINS_USERNAME if jenkins_username is None else jenkins_username
    resolved_api_token = JENKINS_API_TOKEN if jenkins_api_token is None else jenkins_api_token
    authentication_enabled = bool(resolved_username and resolved_api_token)
    module_logger.info(
        f"Preparing Jenkins fetcher (timeout={request_timeout_seconds}s, auth_enabled={authentication_enabled})"
    )
    jenkins_username = resolved_username
    jenkins_api_token = resolved_api_token

    http_opener = create_url_opener(jenkins_username=jenkins_username, jenkins_api_token=jenkins_api_token)
    if http_opener is not None:
        module_logger.debug("Fetcher is configured with a basic-auth opener")
    else:
        module_logger.info("Fetcher is configured without an auth opener")

    def fetch_build_payload(normalized_build_run_url) -> dict[str, Any]:
        module_logger.info(f"Retrieving Jenkins build payload from {normalized_build_run_url}")
        api_tree = (
            "url,subBuilds[url,jobName,jobUrl,buildNumber],"
            "runs[url,jobName,jobUrl,buildNumber],"
            "builds[url,jobName,jobUrl,buildNumber],"
            "actions[downstreamBuilds[url,jobName,jobUrl,buildNumber],"
            "subBuilds[url,jobName,jobUrl,buildNumber],"
            "builds[url,jobName,jobUrl,buildNumber]]"
        )
        api_endpoint_url = f"{normalize_run_url(normalized_build_run_url)}api/json?tree={api_tree}"
        module_logger.debug(f"Jenkins API endpoint URL: {api_endpoint_url}")
        parsed_build_payload = fetch_url_json(target_url=api_endpoint_url, url_opener=http_opener, request_timeout_seconds=request_timeout_seconds)
        module_logger.debug(f"Retrieved and parsed the Jenkins payload from {normalized_build_run_url}")
        return parsed_build_payload

    return fetch_build_payload


def _resolve_child_run_url(item_payload, parent_base_url) -> str | None:
    """Derive a child run URL from a Jenkins item, using fallback fields commonly exposed by multijob plugins."""
    module_logger.debug(f"Trying to derive a child run URL from item keys={list(item_payload.keys())}")
    direct_run_url = item_payload.get("url")
    if isinstance(direct_run_url, str) and direct_run_url.strip():
        if "://" in direct_run_url:
            resolved_child_url = normalize_run_url(direct_run_url)
            module_logger.debug(f"Resolved child URL from absolute 'url' field: {resolved_child_url}")
            return resolved_child_url

        root_url_parts = urlsplit(parent_base_url)
        artifact_relative_path = direct_run_url if direct_run_url.startswith("/") else f"/{direct_run_url}"
        resolved_child_url = normalize_run_url(
            urlunsplit((root_url_parts.scheme, root_url_parts.netloc, artifact_relative_path, "", ""))
        )
        module_logger.debug(f"Resolved child URL from relative 'url' field: {resolved_child_url}")
        return resolved_child_url

    resolved_build_number = item_payload.get("buildNumber")
    if resolved_build_number is None:
        resolved_build_number = item_payload.get("number")
    if isinstance(resolved_build_number, str) and resolved_build_number.isdigit():
        resolved_build_number = int(resolved_build_number)
    if not isinstance(resolved_build_number, int):
        return None

    job_base_url = item_payload.get("jobUrl")
    if isinstance(job_base_url, str) and job_base_url.strip():
        resolved_child_url = normalize_run_url(f"{normalize_run_url(job_base_url)}{resolved_build_number}/")
        module_logger.debug(f"Resolved child URL from 'jobUrl' + 'buildNumber': {resolved_child_url}")
        return resolved_child_url

    job_display_name = item_payload.get("jobName") or item_payload.get("name")
    if isinstance(job_display_name, str) and job_display_name.strip():
        root_url_parts = urlsplit(parent_base_url)
        module_logger.debug(f"Constructing Jenkins job path from name: {job_display_name}")
        job_route_path = compose_job_path(job_display_name)
        module_logger.debug(f"Constructed Jenkins job path: {job_route_path}")
        resolved_child_url = normalize_run_url(
            urlunsplit((root_url_parts.scheme, root_url_parts.netloc, f"/{job_route_path}/{resolved_build_number}/", "", ""))
        )
        module_logger.debug(f"Resolved child URL from 'jobName' + 'buildNumber': {resolved_child_url}")
        return resolved_child_url

    module_logger.debug("Unable to resolve a child run URL from the item")
    return None


def _collect_downstream_urls(build_payload, parent_base_url) -> list[str]:
    """Gather downstream run URLs from common Jenkins or multijob action payload shapes."""
    module_logger.info(f"Gathering downstream URLs for root_url={parent_base_url}")
    collected_downstream_urls: list[str] = []

    def append_urls_from_list(candidate_entries: object) -> None:
        if not isinstance(candidate_entries, list):
            return
        for item_payload in candidate_entries:
            if not isinstance(item_payload, dict):
                continue
            resolved_child_run_url = _resolve_child_run_url(item_payload, parent_base_url=parent_base_url)
            if resolved_child_run_url:
                collected_downstream_urls.append(resolved_child_run_url)

    append_urls_from_list(build_payload.get("runs"))
    append_urls_from_list(build_payload.get("builds"))
    append_urls_from_list(build_payload.get("subBuilds"))

    for action_entry in build_payload.get("actions") or []:
        if not isinstance(action_entry, dict):
            continue
        append_urls_from_list(action_entry.get("downstreamBuilds"))
        append_urls_from_list(action_entry.get("subBuilds"))
        append_urls_from_list(action_entry.get("builds"))

    module_logger.info(f"Gathered {len(collected_downstream_urls)} downstream URL candidates for {parent_base_url}")
    return collected_downstream_urls


def discover_downstream_jobrun_urls(
    pipeline_execution_url,
    jenkins_username = None,
    jenkins_api_token = None,
    max_traversal_depth = 25,
    build_payload_fetcher = None,
) -> list[str]:
    """
    Walk through a Jenkins multijob pipeline run and return the URLs for all downstream job runs.

    Args:
        root_run_url: URL of the parent multijob run.
        username: Jenkins username for basic auth (optional).
        api_token: Jenkins API token or password for basic auth (optional).
        max_depth: Upper bound for recursive traversal depth.
        fetcher: Optional custom fetcher for tests; defaults to the Jenkins API fetcher.
    """
    if max_traversal_depth < 1:
        raise ValueError("max_traversal_depth must be at least 1")

    module_logger.info(
        f"Beginning downstream traversal: root_run_url={pipeline_execution_url}, max_depth={max_traversal_depth}, custom_fetcher={build_payload_fetcher is not None}"
    )

    jenkins_username = JENKINS_USERNAME if jenkins_username is None else jenkins_username
    jenkins_api_token = JENKINS_API_TOKEN if jenkins_api_token is None else jenkins_api_token

    canonical_root_run_url = normalize_run_url(pipeline_execution_url)
    fetch_build = build_payload_fetcher or _create_build_fetcher(jenkins_username=jenkins_username, jenkins_api_token=jenkins_api_token)
    module_logger.debug(f"Traversal starting URL: {canonical_root_run_url}")

    pending_queue: deque[tuple[str, int]] = deque([(canonical_root_run_url, 0)])
    visited_run_urls: set[str] = set()
    discovered_run_urls: list[str] = []
    seen_run_urls: set[str] = set()

    while pending_queue:
        current_run_url, current_depth = pending_queue.popleft()
        module_logger.debug(f"Pulled URL from queue at depth={current_depth}: {current_run_url}")
        if current_run_url in visited_run_urls:
            module_logger.debug(f"Skipping URL that was already visited: {current_run_url}")
            continue
        visited_run_urls.add(current_run_url)

        try:
            payload_bytes = fetch_build(current_run_url)
        except Exception as error_details:
            module_logger.warning(f"Problem while fetching Jenkins run data for {current_run_url}: {error_details}")
            module_logger.warning(f"Stack trace: {traceback.format_exc()}")
            continue

        for child_run_url in _collect_downstream_urls(payload_bytes, parent_base_url=current_run_url):
            if child_run_url not in seen_run_urls:
                discovered_run_urls.append(child_run_url)
                seen_run_urls.add(child_run_url)
                module_logger.info(f"Discovered downstream URL: {child_run_url}")

            if current_depth + 1 < max_traversal_depth and child_run_url not in visited_run_urls:
                pending_queue.append((child_run_url, current_depth + 1))
                module_logger.debug(f"Queued child URL for depth={current_depth + 1}: {child_run_url}")

    module_logger.info(
        f"Downstream traversal complete: visited={len(visited_run_urls)}, discovered={len(discovered_run_urls)}"
    )
    return discovered_run_urls

def discover_jobrun_urls_from_job_url_list(job_url_list: list[str]) -> list:
    module_logger.info(f"Starting latest-jobrun discovery for {len(job_url_list)} Jenkins jobs")
    fetch_job_payload = _create_job_fetcher()
    discovered_jobrun_urls: list[str] = []
    seen_jobrun_urls: set[str] = set()

    for raw_job_url in job_url_list:
        if not isinstance(raw_job_url, str) or not raw_job_url.strip():
            module_logger.warning(f"Skipping invalid Jenkins job URL value: {raw_job_url!r}")
            continue

        canonical_job_url = normalize_run_url(raw_job_url)
        module_logger.info(f"Resolving latest build for Jenkins job: {canonical_job_url}")

        try:
            job_payload = fetch_job_payload(canonical_job_url)
        except Exception as error_details:
            module_logger.warning(f"Problem while fetching Jenkins job data for {canonical_job_url}: {error_details}")
            module_logger.warning(f"Stack trace: {traceback.format_exc()}")
            continue

        latest_run_payload = job_payload.get("lastCompletedBuild") or job_payload.get("lastBuild")
        if not isinstance(latest_run_payload, dict):
            module_logger.warning(f"No latest Jenkins build metadata was found for {canonical_job_url}")
            continue

        latest_jobrun_url = _resolve_child_run_url(latest_run_payload, parent_base_url=canonical_job_url)
        if not latest_jobrun_url:
            module_logger.warning(f"Could not resolve latest Jenkins build URL for {canonical_job_url}")
            continue

        if latest_jobrun_url in seen_jobrun_urls:
            module_logger.debug(f"Skipping duplicate latest jobrun URL: {latest_jobrun_url}")
            continue

        discovered_jobrun_urls.append(latest_jobrun_url)
        seen_jobrun_urls.add(latest_jobrun_url)
        module_logger.info(f"Resolved latest jobrun URL: {latest_jobrun_url}")

    module_logger.info(
        f"Latest-jobrun discovery complete: requested={len(job_url_list)}, resolved={len(discovered_jobrun_urls)}"
    )
    return discovered_jobrun_urls


if __name__ == "__main__":
    pipeline_jobrun_url = ''
    downstream_links = discover_downstream_jobrun_urls(pipeline_jobrun_url)
    if downstream_links:
        print("\n".join(downstream_links))
    else:
        print("No downstream job runs were found.")
