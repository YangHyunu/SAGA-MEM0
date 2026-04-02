from prometheus_client import Counter, Histogram

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP 요청 지연 시간",
    labelnames=["method", "endpoint", "status"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

http_requests_total = Counter(
    "http_requests_total",
    "HTTP 요청 수",
    labelnames=["method", "endpoint", "status"],
)

llm_inference_duration_seconds = Histogram(
    "llm_inference_duration_seconds",
    "LLM 추론 시간",
    labelnames=["model", "provider"],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

llm_tokens_total = Counter(
    "llm_tokens_total",
    "LLM 토큰 사용량",
    labelnames=["model", "type"],
)

memory_operation_duration_seconds = Histogram(
    "memory_operation_duration_seconds",
    "mem0 백엔드 작업 소요 시간",
    labelnames=["backend", "op"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)

memory_events_total = Counter(
    "memory_events_total",
    "mem0 이벤트 수",
    labelnames=["event_type"],
)

lorebook_entries_injected_total = Counter(
    "lorebook_entries_injected_total",
    "로어북 엔트리 주입 수",
    labelnames=["type"],
)

context_build_duration_seconds = Histogram(
    "context_build_duration_seconds",
    "컨텍스트 빌드 전체 소요 시간",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0],
)
