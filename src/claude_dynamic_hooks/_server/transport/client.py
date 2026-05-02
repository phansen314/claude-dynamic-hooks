"""Minimal HTTP client for the router→handler hop.

Returns parsed dict on 2xx with a JSON-object body; returns None on any
failure (timeout, connection refused, non-2xx, oversize, malformed JSON,
non-object body). The router treats None as "handler abstained" or
"handler unreachable" — both coerce to chain-continue. The specific cause
is logged to stderr at the failure site so operators debugging a chain
can distinguish e.g. a 500 from a timeout.
"""
from __future__ import annotations

import json
import sys

import requests


def _log(url: str, cause: str) -> None:
    sys.stderr.write(f"client {url}: {cause}\n")
    sys.stderr.flush()


def post(url: str, body: dict, *, timeout_s: float, max_response_bytes: int) -> dict | None:
    try:
        resp = requests.post(url, json=body, timeout=timeout_s, stream=True)
    except requests.Timeout:
        _log(url, f"timeout after {timeout_s}s")
        return None
    except requests.ConnectionError as e:
        _log(url, f"connection error: {e}")
        return None
    except requests.RequestException as e:
        _log(url, f"request error: {e}")
        return None
    try:
        if not resp.ok:
            _log(url, f"http {resp.status_code}")
            return None
        cl = resp.headers.get("Content-Length")
        if cl is not None and cl.isdigit() and int(cl) > max_response_bytes:
            _log(url, f"oversize (content-length {cl} > {max_response_bytes})")
            return None
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                total += len(chunk)
                if total > max_response_bytes:
                    _log(url, f"oversize (>{max_response_bytes}B)")
                    return None
                chunks.append(chunk)
        except requests.RequestException as e:
            _log(url, f"read error: {e}")
            return None
        body_bytes = b"".join(chunks)
    finally:
        resp.close()
    try:
        obj = json.loads(body_bytes)
    except ValueError:
        _log(url, "malformed JSON response")
        return None
    if not isinstance(obj, dict):
        _log(url, "response body not a JSON object")
        return None
    return obj
