from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import HTTPBasicAuthHandler, HTTPPasswordMgrWithDefaultRealm, build_opener as urllib_build_opener, urlopen


def fetch_url_bytes(target_url, url_opener, request_timeout_seconds) -> bytes:
    """Fetch raw bytes from a URL using an optional opener."""
    if url_opener is not None:
        http_response = url_opener.open(target_url, timeout=request_timeout_seconds)
    else:
        http_response = urlopen(target_url, timeout=request_timeout_seconds)

    with http_response:
        return http_response.read()


def download_url_to_file(source_url, output_file_path, url_opener, request_timeout_seconds) -> None:
    """Download URL content into the requested file path."""
    payload_bytes = fetch_url_bytes(target_url=source_url, url_opener=url_opener, request_timeout_seconds=request_timeout_seconds)
    output_file_path.parent.mkdir(parents=True, exist_ok=True)
    output_file_path.write_bytes(payload_bytes)
def fetch_url_json(target_url, url_opener, request_timeout_seconds) -> dict[str, Any]:
    """Fetch a JSON object from a URL using an optional opener."""
    payload_bytes = fetch_url_bytes(target_url=target_url, url_opener=url_opener, request_timeout_seconds=request_timeout_seconds)
    return json.loads(payload_bytes.decode("utf-8"))


def create_url_opener(jenkins_username, jenkins_api_token):
    """Create a urllib opener with optional basic auth; return None if credentials are absent."""
    if not jenkins_username or not jenkins_api_token:
        return None

    password_manager = HTTPPasswordMgrWithDefaultRealm()
    password_manager.add_password(None, uri="*", user=jenkins_username, passwd=jenkins_api_token)
    return urllib_build_opener(HTTPBasicAuthHandler(password_manager))


def compose_job_path(display_job_name) -> str:
    """Convert a Jenkins job name into a /job/... path that supports nested folders."""
    prepared_job_name = str(display_job_name).replace("»", "/")
    path_segments = [name_segment.strip() for name_segment in prepared_job_name.split("/") if name_segment.strip()]
    encoded_segments = [quote(name_segment, safe="") for name_segment in path_segments]
    return "/".join(f"job/{name_segment}" for name_segment in encoded_segments)


def parse_job_name_and_run_number(run_base_url) -> tuple[str, str]:
    """Read the Jenkins job path and run number from a Jenkins run URL.

    For nested jobs (``/job/folder/job/child/<run>/``), it returns ``folder/child``
    so cache keys and report filenames remain collision-resistant.
    """
    url_parts = [path_part for path_part in urlsplit(run_base_url).path.split("/") if path_part]

    job_run_number = next((path_part for path_part in reversed(url_parts) if path_part.isdigit()), "unknown_run")
    run_number_index = next(
        (entry_index for entry_index in range(len(url_parts) - 1, -1, -1) if url_parts[entry_index].isdigit()),
        None,
    )

    job_name_segments: list[str] = []
    search_upper_bound = run_number_index if run_number_index is not None else len(url_parts)
    for entry_index in range(search_upper_bound - 1):
        if url_parts[entry_index] != "job":
            continue
        name_segment = unquote(url_parts[entry_index + 1]).strip()
        if name_segment:
            job_name_segments.append(name_segment)

    job_display_name = "/".join(job_name_segments) if job_name_segments else "unknown_job"

    return job_display_name, job_run_number


def canonicalize_run_url(raw_url) -> str:
    """Return a canonical Jenkins run URL without query or fragment and with a trailing slash."""
    sanitized_value = str(raw_url).strip()
    url_parts = urlsplit(sanitized_value)
    normalized_path = url_parts.path if url_parts.path.endswith("/") else f"{url_parts.path}/"
    return urlunsplit((url_parts.scheme, url_parts.netloc, normalized_path, "", ""))


