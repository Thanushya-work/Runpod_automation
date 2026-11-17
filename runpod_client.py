"""Small wrapper around the Runpod REST API v2 (https://api.runpod.io/v2)."""
import json
import re
import time
import requests

from settings import API


class RunpodError(Exception):
    pass


class Runpod:
    def __init__(self, api_key):
        if not api_key:
            raise RunpodError("RUNPOD_API_KEY is not set")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})

    # -- low level ----------------------------------------------------------------
    def _request(self, method, path, attempts=5, **kw):
        """Retries network errors, 429 (honours Retry-After) and 5xx. Returns the last response."""
        kw.setdefault("timeout", 60)
        r = None
        for i in range(attempts):
            try:
                r = self.s.request(method, API + path, **kw)
            except requests.RequestException as e:
                if i == attempts - 1:
                    raise RunpodError(f"network error calling Runpod: {type(e).__name__}")
                time.sleep(2 ** i)
                continue
            if r.status_code == 429:
                time.sleep(min(int(r.headers.get("Retry-After", "5")), 60))
                continue
            if r.status_code >= 500 and i < attempts - 1:
                time.sleep(2 ** i)
                continue
            return r
        return r

    # -- catalog --------------------------------------------------------------------
    def gpu_stock(self):
        """{gpu_id: 'NONE'|'LOW'|'MEDIUM'|'HIGH'}; raises RunpodError if unavailable."""
        r = self._request("GET", "/catalog/gpus", params={"include": "AVAILABILITY", "product": "POD"})
        if not r.ok:
            raise RunpodError(f"catalog lookup failed: HTTP {r.status_code}")
        out = {}

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("id"), str) and "availability" in node:
                    out[node["id"]] = node["availability"]
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(r.json())
        return out

    # -- pods -----------------------------------------------------------------------
    def create_pod(self, body):
        """Returns the raw response so the caller can branch on the status code."""
        return self._request("POST", "/pods", json=body)

    def list_pods(self):
        r = self._request("GET", "/pods")
        if not r.ok:
            raise RunpodError(f"listing pods failed: HTTP {r.status_code}")
        data = r.json()
        return data.get("pods", []) if isinstance(data, dict) else data

    def terminate(self, pod_id):
        r = self._request("DELETE", f"/pods/{pod_id}")
        if r.status_code not in (200, 202, 204, 404):
            raise RunpodError(f"terminate failed: HTTP {r.status_code}")
        return True

    def get_logs(self, pod_id, max_seconds=25, max_bytes=400_000):
        """Reads the pod log stream for a bounded time and returns plain text ('' on failure)."""
        buf, size = [], 0
        deadline = time.time() + max_seconds
        try:
            with self.s.get(f"{API}/pods/{pod_id}/logs", stream=True, timeout=(10, 15)) as r:
                if not r.ok:
                    return ""
                for line in r.iter_lines(decode_unicode=True):
                    if line is None:
                        continue
                    buf.append(line)
                    size += len(line)
                    if time.time() > deadline or size > max_bytes:
                        break
        except requests.RequestException:
            pass  # keep whatever we already read
        return normalize_logs("\n".join(buf))


def normalize_logs(text):
    """Accept plain text, SSE ('data: ...') or JSON-wrapped logs; return plain text lines."""
    t = text.strip()
    if t[:1] in "{[":
        try:
            parts = []

            def collect(n):
                if isinstance(n, str):
                    parts.append(n)
                elif isinstance(n, dict):
                    for v in n.values():
                        collect(v)
                elif isinstance(n, list):
                    for v in n:
                        collect(v)

            collect(json.loads(t))
            return "\n".join(parts)
        except ValueError:
            pass
    return re.sub(r"^data: ?", "", text, flags=re.MULTILINE)
