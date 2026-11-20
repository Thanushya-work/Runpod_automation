"""Small wrapper around the Runpod REST API v2 (https://api.runpod.io/v2)."""
import json
import re
import time
import requests

from settings import API, MIN_CUDA_VERSION


class RunpodError(Exception):
    pass


_LEVELS = ["NONE", "LOW", "MEDIUM", "HIGH"]


def is_rtx(gpu):
    """True when the Runpod display name starts with 'RTX' (e.g. 'RTX A5000', 'RTX 4090', 'RTX PRO 6000')."""
    return str(gpu.get("name", "")).startswith("RTX")


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
    def catalog_raw(self, cloud="SECURE", count=1, min_cuda=MIN_CUDA_VERSION):
        """Raw /catalog/gpus response with live availability for exactly the pod shape we launch.

        All filters are applied server-side by Runpod: 'cloud' (defaults to SECURE on the API side
        if omitted), 'count' (GPUs per pod) and 'minCudaVersion' (hosts with an older driver are
        excluded, since we could not launch on them anyway). No-cache headers plus a throwaway
        query parameter make sure no proxy/CDN hands back a stale copy.
        """
        params = {"include": "AVAILABILITY", "product": "POD", "cloud": cloud, "count": count,
                  "_": str(time.time_ns())}
        if min_cuda:
            params["minCudaVersion"] = min_cuda
        r = self._request("GET", "/catalog/gpus", params=params,
                          headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"})
        if not r.ok:
            raise RunpodError(f"catalog lookup failed: HTTP {r.status_code}")
        return r

    def gpu_stock(self, cloud="SECURE", rtx_only=False, by_name=False, count=1, min_cuda=MIN_CUDA_VERSION):
        """{gpu_id: 'NONE'|'LOW'|'MEDIUM'|'HIGH'} for the given cloud; raises RunpodError if unavailable.
        by_name=True keys the result by the short display name ('RTX A5000') instead of the id.

        The top-level 'availability' is already scoped by Runpod to the requested cloud / count /
        CUDA filter, so it is the value to use. 'dataCenters' is the same thing broken down per DC.
        """
        data = self.catalog_raw(cloud, count, min_cuda).json()
        gpus = data.get("gpus", []) if isinstance(data, dict) else data
        out = {}
        for g in gpus:
            gid = g.get("id")
            if not isinstance(gid, str):
                continue
            if rtx_only and not is_rtx(g):
                continue
            level = g.get("availability")
            if level not in _LEVELS:     # field missing: fall back to the best per-DC level
                dcs = [dc.get("availability", "NONE") for dc in g.get("dataCenters") or []]
                level = max(dcs, key=lambda l: _LEVELS.index(l) if l in _LEVELS else 0, default="NONE")
            out[g.get("name") or gid if by_name else gid] = level
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


def _log_line(raw):
    """One log line -> plain text, or None to drop it. Handles SSE framing ('id:', 'event:',
    'data: ...') and the per-line JSON records Runpod streams: {"source":..,"line":"..","ts":".."}."""
    s = raw.strip()
    if not s or s.startswith(("id:", "event:", "retry:", ":")):
        return None
    if s.startswith("data:"):
        s = s[5:].strip()
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except ValueError:
            return s
        if isinstance(obj, dict):
            for key in ("line", "message", "msg", "log", "text"):
                if isinstance(obj.get(key), str):
                    return obj[key]
        return None
    return s


def normalize_logs(text):
    """Accept plain text, SSE ('data: ...'), per-line JSON records or one JSON document; return plain text lines."""
    t = text.strip()
    lines = t.splitlines()
    if len(lines) > 1 or t.startswith(("id:", "data:", "event:")):
        try:
            json.loads(t)            # a single (pretty-printed) JSON document: handled below
        except ValueError:
            return "\n".join(l for l in map(_log_line, lines) if l is not None)
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


if __name__ == "__main__":
    # python runpod_client.py            -> GPUs named 'RTX ...' in stock on Secure Cloud
    # python runpod_client.py --all      -> every GPU in stock on Secure Cloud
    # python runpod_client.py --community -> Community Cloud instead of Secure
    # python runpod_client.py --any-cuda  -> do not filter by MIN_CUDA_VERSION (like the console default)
    # python runpod_client.py --debug    -> also print HTTP cache headers and per-data-center stock
    import os
    import sys
    from datetime import datetime
    cloud = "COMMUNITY" if "--community" in sys.argv else "SECURE"
    min_cuda = None if "--any-cuda" in sys.argv else MIN_CUDA_VERSION
    client = Runpod(os.environ.get("RUNPOD_API_KEY"))
    if "--debug" in sys.argv:
        r = client.catalog_raw(cloud, 1, min_cuda)
        print("fetched", datetime.now().isoformat(timespec="seconds"), "->", r.url.split("?")[0])
        for h in ("Date", "Age", "Cache-Control", "ETag", "Last-Modified", "CF-Cache-Status", "X-Cache"):
            if h in r.headers:
                print(f"  {h}: {r.headers[h]}")
        for g in r.json().get("gpus", []):
            if "--all" in sys.argv or is_rtx(g):
                dcs = ", ".join(f'{d.get("id")}={d.get("availability")}' for d in g.get("dataCenters") or [])
                print(f'  {g.get("name")}: {g.get("availability")}  [{dcs}]')
        print()
    stock = client.gpu_stock(cloud=cloud, rtx_only="--all" not in sys.argv, by_name=True, min_cuda=min_cuda)
    free = {k: v for k, v in sorted(stock.items()) if v != "NONE"}
    print(f"[{datetime.now():%H:%M:%S}] {cloud}, CUDA >= {min_cuda or 'any'}")
    ids = {} if not free else {g.get("name"): g.get("id") for g in client.catalog_raw(cloud, 1, min_cuda).json().get("gpus", [])}
    print("\n".join(f"{k:<28} {v:<7} gpu_override: {ids.get(k, k)}" for k, v in free.items())
          or f"No GPUs in stock on {cloud} cloud right now.")
