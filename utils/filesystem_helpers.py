from __future__ import annotations
import hashlib
import re
def truncate_component(component_text, max_length) -> str:
    """Trim an overlong component while keeping deterministic uniqueness."""
    if len(component_text) <= max_length:
        return component_text
    sha_digest = hashlib.sha1(component_text.encode("utf-8")).hexdigest()[:8]
    prefix_length = max_length - len(sha_digest) - 1
    if prefix_length <= 0:
        return sha_digest[:max_length]
    return f"{component_text[:prefix_length]}-{sha_digest}"
def make_safe_component(raw_value, default_value, allow_dots = True) -> str:
    """Turn arbitrary text into a filesystem-safe path component."""
    sanitized_value = str(raw_value).strip()
    sanitized_value = re.sub(r"\s+", "_", sanitized_value)
    sanitized_pattern = r"[^A-Za-z0-9._-]" if allow_dots else r"[^A-Za-z0-9_-]"
    sanitized_value = re.sub(sanitized_pattern, "_", sanitized_value)
    strip_chars = "._-" if allow_dots else "_-"
    sanitized_value = re.sub(r"_+", "_", sanitized_value).strip(strip_chars)
    return sanitized_value or default_value
