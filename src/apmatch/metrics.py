"""Prometheus text exposition without extra dependencies."""

from __future__ import annotations

import threading
from collections import defaultdict

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5)


def _labels(d: dict) -> str:
    if not d:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(d.items())) + "}"


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = defaultdict(int)  # (route, status)
        self.actions = defaultdict(int)  # (exception type, action)
        self.gauges: dict[tuple[str, tuple], tuple[str, float]] = {}
        self.buckets = [0] * len(LATENCY_BUCKETS)
        self.lat_sum = 0.0
        self.lat_count = 0

    def observe_request(self, route: str, status: int, latency_s: float) -> None:
        with self.lock:
            self.requests[(route, str(status))] += 1
            for i, b in enumerate(LATENCY_BUCKETS):
                if latency_s <= b:
                    self.buckets[i] += 1
            self.lat_sum += latency_s
            self.lat_count += 1

    def action(self, etype: str, action: str) -> None:
        with self.lock:
            self.actions[(etype, action)] += 1

    def set_gauge(self, name: str, help_text: str, value: float, **labels) -> None:
        with self.lock:
            self.gauges[(name, tuple(sorted(labels.items())))] = (help_text, value)

    def render(self) -> str:
        out: list[str] = []
        with self.lock:

            def family(name, kind, help_text, samples):
                out.append(f"# HELP {name} {help_text}")
                out.append(f"# TYPE {name} {kind}")
                for labels, value in samples:
                    out.append(f"{name}{_labels(labels)} {value}")

            family(
                "apmatch_http_requests_total",
                "counter",
                "HTTP requests by route and status.",
                [({"route": r, "status": s}, v) for (r, s), v in sorted(self.requests.items())],
            )
            family(
                "apmatch_exception_actions_total",
                "counter",
                "Exception actions taken through the API, by exception type and action.",
                [({"type": t, "action": a}, v) for (t, a), v in sorted(self.actions.items())],
            )
            names: dict[str, list] = {}
            helps: dict[str, str] = {}
            for (name, labels), (help_text, value) in sorted(self.gauges.items()):
                names.setdefault(name, []).append((dict(labels), value))
                helps[name] = help_text
            for name, samples in names.items():
                family(name, "gauge", helps[name], samples)
            out.append("# HELP apmatch_http_request_duration_seconds Request latency.")
            out.append("# TYPE apmatch_http_request_duration_seconds histogram")
            for b, c in zip(LATENCY_BUCKETS, self.buckets, strict=True):
                out.append(f'apmatch_http_request_duration_seconds_bucket{{le="{b}"}} {c}')
            out.append(f'apmatch_http_request_duration_seconds_bucket{{le="+Inf"}} {self.lat_count}')
            out.append(f"apmatch_http_request_duration_seconds_sum {self.lat_sum:.6f}")
            out.append(f"apmatch_http_request_duration_seconds_count {self.lat_count}")
        return "\n".join(out) + "\n"
