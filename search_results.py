"""Domain selection and conservative search-result merging (no network I/O)."""
import ipaddress
import re
from itertools import zip_longest
from urllib.parse import urlsplit, urlunsplit


def normalize_domains(domains: list[str] | None) -> list[str]:
    normalized = []
    for value in domains or []:
        try:
            domain = value.strip().encode("idna").decode("ascii").lower().removesuffix(".")
        except UnicodeError as exc:
            raise ValueError("域名编码无效") from exc
        if (not domain or len(domain) > 253 or
                any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in domain.split("."))):
            raise ValueError("请填写裸域名，例如 docs.python.org；不支持 URL、端口或通配符")
        if domain not in normalized:
            normalized.append(domain)
    return normalized


def url_identity(url: str) -> tuple[str, str]:
    """Return normalized URL and hostname, rejecting ambiguous/unusable links."""
    if any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url):
        raise ValueError("无效 URL")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
        raise ValueError("需要不含用户信息的 HTTP(S) URL")
    host = parsed.hostname
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        host = normalize_domains([host])[0]
    port = parsed.port  # Validate malformed/out-of-range ports too.
    authority = f"[{host}]" if ":" in host else host
    if port is not None and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
        authority += f":{port}"
    # Preserve scheme, path case, trailing slash, encoding and query exactly.
    key = urlunsplit((parsed.scheme, authority, parsed.path or "/", parsed.query, ""))
    return key, host


def domain_matches(host: str, domain: str) -> bool:
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return host == domain or host.endswith("." + domain)
    return host == domain


def select_results(items: list[dict], engine: str, include: list[str], exclude: list[str]) -> tuple[list[dict], int]:
    selected = []
    rejected = 0
    for rank, item in enumerate(items, 1):
        try:
            key, host = url_identity(item["url"])
        except (ValueError, UnicodeError):
            rejected += 1
            continue
        if ((include and not any(domain_matches(host, d) for d in include)) or
                any(domain_matches(host, d) for d in exclude)):
            rejected += 1
            continue
        selected.append({**item, "canonical_url": key,
                         "provenance": [{"engine": engine, "rank": rank, "url": item["url"]}]})
    return selected, rejected


def merge_results(groups: list[list[dict]], limit: int) -> list[dict]:
    """Interleave engine ranks fairly; keep provenance even after reaching limit."""
    merged: dict[str, dict] = {}
    for row in zip_longest(*groups):
        for item in row:
            if item is None:
                continue
            key = item.get("canonical_url") or url_identity(item["url"])[0]
            if key not in merged:
                merged[key] = {**item, "canonical_url": key, "provenance": []}
            target = merged[key]
            for record in item.get("provenance", []):
                if record not in target["provenance"]:
                    target["provenance"].append(record.copy())
            if not target.get("snippet") and item.get("snippet"):
                target["snippet"] = item["snippet"]
    return list(merged.values())[:limit]
