#!/usr/bin/env python3
"""Load, concurrency, and stress testing harness for Namma CivicLens.

Measures throughput, latencies (avg, med, p95, p99), HTTP status distribution,
resource usage, and data integrity across all workloads required in Section 8.
"""

import argparse
import concurrent.futures
import io
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

JPEG_BYTES = (
    bytes([0xFF, 0xD8, 0xFF, 0xE0, 0, 16, 0x4A, 0x46, 0x49, 0x46, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0])
    + (b"L" * 64_000)
    + bytes([0xFF, 0xD9])
)


@dataclass
class WorkloadMetrics:
    name: str
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    status_distribution: dict[int, int] = field(default_factory=dict)
    avg_latency_ms: float = 0.0
    median_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    throughput_rps: float = 0.0
    timeout_count: int = 0
    memory_mb: float = 0.0
    cpu_percent: float = 0.0
    database_errors: int = 0
    data_integrity_errors: int = 0
    elapsed_seconds: float = 0.0


def percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    k = (len(data) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return data[int(k)]
    d0 = data[int(f)] * (c - k)
    d1 = data[int(c)] * (k - f)
    return d0 + d1


import math


class StressHarness:
    def __init__(self, base_url: str, staff_user: str = "teststaff", staff_pass: str = "TestPass1234!"):
        self.base = base_url.rstrip("/")
        self.staff_user = staff_user
        self.staff_pass = staff_pass
        self.session = None

    def _http_request(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        headers: dict | None = None,
        timeout: float = 15.0,
    ) -> tuple[int, bytes, float]:
        url = self.base + path
        hdrs = dict(headers or {})
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
        except (TimeoutError, urllib.error.URLError) as exc:
            elapsed = (time.perf_counter() - start) * 1000.0
            return 0, str(exc).encode(), elapsed
        elapsed = (time.perf_counter() - start) * 1000.0
        return status, body, elapsed

    def _measure_resources(self) -> tuple[float, float]:
        if not psutil:
            return 0.0, 0.0
        proc = psutil.Process(os.getpid())
        mem = proc.memory_info().rss / (1024 * 1024)
        cpu = psutil.cpu_percent(interval=0.1)
        return mem, cpu

    def _compile_metrics(self, name: str, results: list[tuple[int, bytes, float]], elapsed: float) -> WorkloadMetrics:
        m = WorkloadMetrics(name=name, total_requests=len(results), elapsed_seconds=elapsed)
        latencies = []
        for status, body, lat in results:
            m.status_distribution[status] = m.status_distribution.get(status, 0) + 1
            if status in (200, 201, 202, 204):
                m.successful_requests += 1
            elif status == 0:
                m.timeout_count += 1
                m.failed_requests += 1
            else:
                m.failed_requests += 1
            if status >= 500:
                m.database_errors += 1
            latencies.append(lat)

        if latencies:
            latencies.sort()
            m.avg_latency_ms = round(statistics.mean(latencies), 2)
            m.median_latency_ms = round(statistics.median(latencies), 2)
            m.p95_latency_ms = round(percentile(latencies, 95), 2)
            m.p99_latency_ms = round(percentile(latencies, 99), 2)
        if elapsed > 0:
            m.throughput_rps = round(len(results) / elapsed, 2)

        mem, cpu = self._measure_resources()
        m.memory_mb = round(mem, 1)
        m.cpu_percent = round(cpu, 1)
        return m

    # ---------- Workload 1: Concurrent complaint creation (50 clients) ----------
    def workload_complaint_creation(self, concurrency: int = 50) -> tuple[WorkloadMetrics, list[dict]]:
        barrier = threading.Barrier(concurrency)
        results = []
        created_receipts = []
        lock = threading.Lock()

        def worker(idx):
            payload = json.dumps({
                "category": "Potholes",
                "mediaType": "photo",
                "description": f"Stress complaint #{idx}",
                "gps": {"lat": 13.0358, "lng": 77.597, "sourceType": "gps", "accuracy": 12.0},
            }).encode()
            barrier.wait()
            status, body, lat = self._http_request("POST", "/complaints", data=payload, headers={"Content-Type": "application/json"})
            results.append((status, body, lat))
            if status == 201:
                try:
                    data = json.loads(body)
                    with lock:
                        created_receipts.append(data)
                except Exception:
                    pass

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(worker, i) for i in range(concurrency)]
            for f in futures:
                f.result()
        elapsed = time.perf_counter() - start

        m = self._compile_metrics("Concurrent complaint creation (50 clients)", results, elapsed)
        # Integrity check: verify all public IDs are unique
        pids = [r["id"] for r in created_receipts]
        if len(set(pids)) != len(pids):
            m.data_integrity_errors += (len(pids) - len(set(pids)))
        return m, created_receipts

    # ---------- Workload 2: Public complaint tracking (100 requests) ----------
    def workload_public_tracking(self, public_id: str, concurrency: int = 100) -> WorkloadMetrics:
        barrier = threading.Barrier(concurrency)
        results = []

        def worker():
            barrier.wait()
            status, body, lat = self._http_request("GET", f"/complaints/{public_id}")
            results.append((status, body, lat))

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(worker) for _ in range(concurrency)]
            for f in futures:
                f.result()
        elapsed = time.perf_counter() - start
        return self._compile_metrics("Public complaint tracking (100 requests)", results, elapsed)

    # ---------- Workload 3: Evidence uploads (20 concurrent) ----------
    def workload_evidence_uploads(self, complaints: list[dict], concurrency: int = 20) -> WorkloadMetrics:
        count = min(len(complaints), concurrency)
        barrier = threading.Barrier(count)
        results = []

        def worker(idx):
            comp = complaints[idx]
            pid = comp["id"]
            token = comp["uploadToken"]
            # Build multipart body
            boundary = "----WebKitFormBoundaryStressTest"
            body = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="evidence.jpg"\r\n'
                f"Content-Type: image/jpeg\r\n\r\n"
            ).encode() + JPEG_BYTES + f"\r\n--{boundary}--\r\n".encode()

            barrier.wait()
            status, resp, lat = self._http_request(
                "POST",
                f"/complaints/{pid}/evidence",
                data=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Authorization": f"Bearer {token}",
                },
            )
            results.append((status, resp, lat))

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
            futures = [pool.submit(worker, i) for i in range(count)]
            for f in futures:
                f.result()
        elapsed = time.perf_counter() - start
        return self._compile_metrics("Evidence uploads (20 concurrent)", results, elapsed)

    # ---------- Workload 4: Notification creation (100 synthetic events) ----------
    def workload_notification_creation(self, count: int = 100) -> tuple[WorkloadMetrics, str]:
        # Create an inbox
        _, body, _ = self._http_request("POST", "/notifications/inboxes")
        inbox_token = json.loads(body)["inboxToken"]
        headers = {"Authorization": f"Bearer {inbox_token}"}

        results = []
        start = time.perf_counter()
        for i in range(count):
            status, resp, lat = self._http_request("POST", "/notifications/test", headers=headers)
            results.append((status, resp, lat))
        elapsed = time.perf_counter() - start
        return self._compile_metrics("Notification creation (100 synthetic events)", results, elapsed), inbox_token

    # ---------- Workload 5: Notification inbox reads (100 simultaneous) ----------
    def workload_inbox_reads(self, inbox_token: str, concurrency: int = 100) -> WorkloadMetrics:
        barrier = threading.Barrier(concurrency)
        results = []
        headers = {"Authorization": f"Bearer {inbox_token}"}

        def worker():
            barrier.wait()
            status, body, lat = self._http_request("GET", "/notifications?limit=20", headers=headers)
            results.append((status, body, lat))

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(worker) for _ in range(concurrency)]
            for f in futures:
                f.result()
        elapsed = time.perf_counter() - start
        return self._compile_metrics("Notification inbox reads (100 requests)", results, elapsed)

    # ---------- Workload 6: Staff authentication attempts (50 controlled) ----------
    def workload_staff_auth(self, attempts: int = 50) -> WorkloadMetrics:
        barrier = threading.Barrier(attempts)
        results = []

        def worker(idx):
            # Alternate valid and invalid passwords
            pw = self.staff_pass if idx % 2 == 0 else "WrongPassword123!"
            payload = json.dumps({"username": self.staff_user, "password": pw}).encode()
            barrier.wait()
            status, body, lat = self._http_request("POST", "/staff/sessions", data=payload, headers={"Content-Type": "application/json"})
            results.append((status, body, lat))

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=attempts) as pool:
            futures = [pool.submit(worker, i) for i in range(attempts)]
            for f in futures:
                f.result()
        elapsed = time.perf_counter() - start
        m = self._compile_metrics("Staff authentication attempts (50 controlled)", results, elapsed)
        # For staff auth, 200 is valid login, 401 is rejected invalid password (controlled)
        controlled_ok = sum(1 for s, _, _ in results if s in (200, 401))
        m.successful_requests = controlled_ok
        m.failed_requests = len(results) - controlled_ok
        return m

    # ---------- Workload 7: Database mixed read/write (50 concurrent workers) ----------
    def workload_mixed_db(self, seeded_id: str, inbox_token: str, concurrency: int = 50) -> WorkloadMetrics:
        barrier = threading.Barrier(concurrency)
        results = []
        inbox_hdrs = {"Authorization": f"Bearer {inbox_token}"}

        def worker(idx):
            barrier.wait()
            op = idx % 4
            if op == 0:
                payload = json.dumps({
                    "category": "Drainage or waterlogging",
                    "mediaType": "photo",
                    "description": f"Mixed worker #{idx}",
                    "gps": {"lat": 13.04, "lng": 77.60, "sourceType": "gps", "accuracy": 10.0},
                }).encode()
                r = self._http_request("POST", "/complaints", data=payload, headers={"Content-Type": "application/json"})
            elif op == 1:
                r = self._http_request("GET", f"/complaints/{seeded_id}")
            elif op == 2:
                r = self._http_request("GET", "/notifications/unread-count", headers=inbox_hdrs)
            else:
                r = self._http_request("GET", "/health")
            results.append(r)

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(worker, i) for i in range(concurrency)]
            for f in futures:
                f.result()
        elapsed = time.perf_counter() - start
        return self._compile_metrics("Database mixed read/write (50 workers)", results, elapsed)

    # ---------- Workload 8: Repeated API workload (1,000 requests) ----------
    def workload_repeated_api(self, seeded_id: str, total_requests: int = 1000, concurrency: int = 25) -> WorkloadMetrics:
        results = []
        lock = threading.Lock()
        counter = [0]

        def worker():
            while True:
                with lock:
                    if counter[0] >= total_requests:
                        break
                    idx = counter[0]
                    counter[0] += 1

                op = idx % 3
                if op == 0:
                    r = self._http_request("GET", f"/complaints/{seeded_id}")
                elif op == 1:
                    r = self._http_request("GET", "/health")
                else:
                    r = self._http_request("GET", "/notifications/config")
                with lock:
                    results.append(r)

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(worker) for _ in range(concurrency)]
            for f in futures:
                f.result()
        elapsed = time.perf_counter() - start
        return self._compile_metrics("Repeated API workload (1,000 requests)", results, elapsed)

    # ---------- Burst Test: Sudden load of 100 concurrent requests ----------
    def workload_burst_test(self, seeded_id: str, concurrency: int = 100) -> WorkloadMetrics:
        barrier = threading.Barrier(concurrency)
        results = []

        def worker(idx):
            barrier.wait()
            # 50% tracking, 50% health check
            if idx % 2 == 0:
                r = self._http_request("GET", f"/complaints/{seeded_id}")
            else:
                r = self._http_request("GET", "/health")
            results.append(r)

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(worker, i) for i in range(concurrency)]
            for f in futures:
                f.result()
        elapsed = time.perf_counter() - start
        return self._compile_metrics("Burst test (100 simultaneous requests)", results, elapsed)

    # ---------- Sustained Workload: 5 minutes continuous traffic ----------
    def workload_sustained_5min(self, seeded_id: str, duration_seconds: int = 300, concurrency: int = 10) -> WorkloadMetrics:
        results = []
        lock = threading.Lock()
        stop_event = threading.Event()

        def worker(idx):
            local_i = 0
            while not stop_event.is_set():
                local_i += 1
                op = (idx + local_i) % 4
                if op == 0:
                    r = self._http_request("GET", f"/complaints/{seeded_id}")
                elif op == 1:
                    r = self._http_request("GET", "/health")
                elif op == 2:
                    r = self._http_request("GET", "/notifications/config")
                else:
                    p = json.dumps({
                        "category": "Garbage accumulation",
                        "mediaType": "photo",
                        "description": f"Sustained test w{idx} #{local_i}",
                        "gps": {"lat": 13.036, "lng": 77.598, "sourceType": "gps", "accuracy": 15.0},
                    }).encode()
                    r = self._http_request("POST", "/complaints", data=p, headers={"Content-Type": "application/json"})
                with lock:
                    results.append(r)
                time.sleep(0.05)  # smooth pacing

        print(f"Starting sustained workload for {duration_seconds} seconds ({duration_seconds/60:.1f} minutes)...")
        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(worker, i) for i in range(concurrency)]
            # Wait for duration
            time.sleep(duration_seconds)
            stop_event.set()
            for f in futures:
                f.result()
        elapsed = time.perf_counter() - start
        return self._compile_metrics(f"Sustained workload ({duration_seconds/60:.0f} minutes continuous)", results, elapsed)


def print_table(metrics_list: list[WorkloadMetrics]):
    header = f"{'Workload':<42} | {'Reqs':>6} | {'Success':>7} | {'Fail':>5} | {'Throughput':>10} | {'Avg (ms)':>8} | {'p50 (ms)':>8} | {'p95 (ms)':>8} | {'p99 (ms)':>8}"
    sep = "-" * len(header)
    print("\n" + sep)
    print(header)
    print(sep)
    for m in metrics_list:
        print(f"{m.name:<42} | {m.total_requests:>6} | {m.successful_requests:>7} | {m.failed_requests:>5} | {m.throughput_rps:>8.1f}/s | {m.avg_latency_ms:>8.2f} | {m.median_latency_ms:>8.2f} | {m.p95_latency_ms:>8.2f} | {m.p99_latency_ms:>8.2f}")
    print(sep + "\n")


def main():
    parser = argparse.ArgumentParser(description="Namma CivicLens Load and Stress Test Harness")
    parser.add_argument("--api", default="http://127.0.0.1:8010/api/v1", help="Base API URL")
    parser.add_argument("--duration", type=int, default=300, help="Sustained test duration in seconds (default 300)")
    parser.add_argument("--skip-sustained", action="store_true", help="Skip 5-minute sustained test")
    parser.add_argument("--output", default="load_stress_report.json", help="Output JSON path")
    args = parser.parse_args()

    harness = StressHarness(args.api)
    print(f"Connecting to API at {args.api}...")
    status, body, lat = harness._http_request("GET", "/health")
    if status != 200:
        print(f"ERROR: API is unreachable at {args.api} (status {status})")
        sys.exit(1)
    print(f"API is healthy (health check latency {lat:.2f} ms).\n")

    all_metrics = []

    # 1. Complaint creation
    print("Executing Workload 1: Concurrent complaint creation (50 clients)...")
    m1, receipts = harness.workload_complaint_creation(50)
    all_metrics.append(m1)
    seeded_id = receipts[0]["id"] if receipts else "NCL-2026-DEMO01"

    # 2. Public tracking
    print("Executing Workload 2: Public complaint tracking (100 requests)...")
    m2 = harness.workload_public_tracking(seeded_id, 100)
    all_metrics.append(m2)

    # 3. Evidence uploads
    print("Executing Workload 3: Evidence uploads (20 concurrent)...")
    m3 = harness.workload_evidence_uploads(receipts, 20)
    all_metrics.append(m3)

    # 4. Notification creation
    print("Executing Workload 4: Notification creation (100 synthetic events)...")
    m4, inbox_token = harness.workload_notification_creation(100)
    all_metrics.append(m4)

    # 5. Inbox reads
    print("Executing Workload 5: Notification inbox reads (100 requests)...")
    m5 = harness.workload_inbox_reads(inbox_token, 100)
    all_metrics.append(m5)

    # 6. Staff auth
    print("Executing Workload 6: Staff authentication attempts (50 controlled)...")
    m6 = harness.workload_staff_auth(50)
    all_metrics.append(m6)

    # 7. Mixed DB workload
    print("Executing Workload 7: Database mixed read/write (50 workers)...")
    m7 = harness.workload_mixed_db(seeded_id, inbox_token, 50)
    all_metrics.append(m7)

    # 8. Repeated API workload
    print("Executing Workload 8: Repeated API workload (1,000 requests)...")
    m8 = harness.workload_repeated_api(seeded_id, 1000, 25)
    all_metrics.append(m8)

    # 9. Burst test
    print("Executing Burst Test: 100 sudden requests...")
    m_burst = harness.workload_burst_test(seeded_id, 100)
    all_metrics.append(m_burst)

    # 10. Sustained workload (5 min)
    if not args.skip_sustained:
        print(f"Executing Sustained Workload ({args.duration}s)...")
        m_sustained = harness.workload_sustained_5min(seeded_id, args.duration, concurrency=10)
        all_metrics.append(m_sustained)

    print_table(all_metrics)

    # Save JSON report
    report_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "api_url": args.api,
        "workloads": [asdict(m) for m in all_metrics],
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
    print(f"Full report written to {args.output}")


if __name__ == "__main__":
    main()

