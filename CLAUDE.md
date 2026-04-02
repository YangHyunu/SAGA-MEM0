# Yang-Ban: RisuAI Character Proxy with Observability

RisuAI charx 캐릭터/모듈 기반 RP 프록시 서버. 플러그형 메모리 시스템, 구조화 로깅, 분산 트레이싱, 메트릭 수집을 학습하며 구축하는 프로젝트.

## Project Overview

- **Purpose**: RisuAI 클라이언트 → FastAPI Proxy (charx 파싱 + 로어북 주입 + 메모리) → LLM Provider
- **Core Focus**: 로깅(structlog), 트레이싱(OpenTelemetry + Langfuse), 메트릭(Prometheus + Grafana) 학습
- **Memory**: mem0ai + Qdrant(벡터 스토어), SQLite(앱 DB/개발), 경량 Curator
- **Client**: RisuAI (오픈소스 RP/챗봇 프론트엔드)
- **Content**: charx/risum 캐릭터 모듈 — Realm(realm.risuai.net)에서 배포되는 캐릭터 팩 활용

## Architecture Reference

컨텍스트 빌드 파이프라인, mem0 설정, 캐싱 전략, 경량 Curator 설계는 별도 문서 참조:
- **[SAGA_MEM0.md](./SAGA_MEM0.md)** — RISU_ENE(SAGA v3.0) 패턴을 mem0 기반으로 재설계한 아키텍처 문서

## Tech Stack

- **Runtime**: Python 3.13+, FastAPI, uvicorn + uvloop
- **Memory**: mem0ai + Qdrant(벡터 스토어), SQLite(앱 DB/개발), PostgreSQL(프로덕션)
- **Logging**: structlog (구조화 로깅)
- **Tracing**: OpenTelemetry SDK + Langfuse (LLM observability)
- **Metrics**: prometheus-client + Grafana
- **LLM**: OpenAI / Google Gemini / Anthropic Claude (multi-provider)
- **Package Manager**: uv
- **Containerization**: Docker + Docker Compose

## Critical Rules

### Import Rules
- 모든 import는 **파일 최상단**에 위치 — 함수/클래스 내부 import 금지

### Logging Rules
- **structlog** 전용 — print() 또는 stdlib logging 직접 사용 금지
- 이벤트명은 **lowercase_with_underscores** (예: `"chat_request_received"`)
- **f-string 금지** — 변수는 kwargs로 전달: `logger.info("request_processed", duration=dur, model=model)`
- 예외 로깅은 `logger.exception()` 사용 (`logger.error()` 대신)
- 컨텍스트 바인딩 필수: request_id, session_id, user_id

### Tracing Rules
- 모든 LLM 호출에 **Langfuse 트레이싱** 활성화
- OpenTelemetry span으로 주요 작업 단위 감싸기
- span에 적절한 attributes 추가 (model, token_count, latency 등)

### Retry Rules
- **tenacity** 라이브러리로 재시도 로직 구현
- 지수 백오프: `@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))`
- 429 (Rate Limit) 에러는 반드시 재시도 대상에 포함

### FastAPI Rules
- 모든 라우트에 rate limiting 데코레이터
- 서비스/DB/인증은 dependency injection
- 모든 DB 작업은 async
- 응답 스키마는 Pydantic 모델로 정의

### Caching Rules
- **성공 응답만** 캐싱 — 에러 응답 캐싱 금지
- 데이터 변동성에 따라 적절한 TTL 설정

## Code Style

- `async def` for I/O operations
- 모든 함수에 type hints
- Pydantic 모델 > raw dict
- 파일명: lowercase_with_underscores (예: `chat_routes.py`)
- RORO 패턴 (Receive an Object, Return an Object)
- 에러 처리는 함수 앞부분에서 early return / guard clause
- happy path는 함수 마지막에 배치

## Project Structure

```
Yang-Ban/
├── app/
│   ├── api/
│   │   └── v1/
│   │       ├── chat.py              # RisuAI 호환 /v1/chat/completions
│   │       ├── characters.py        # charx 업로드/관리 엔드포인트
│   │       ├── memory.py            # 메모리 CRUD 엔드포인트
│   │       └── router.py            # API 라우터 집합
│   ├── core/
│   │   ├── config.py                # Pydantic Settings 환경 설정
│   │   ├── logging.py               # structlog 설정
│   │   ├── tracing.py               # OpenTelemetry + Langfuse 설정
│   │   ├── metrics.py               # Prometheus 메트릭 정의
│   │   ├── middleware.py             # 로깅/메트릭/트레이싱 미들웨어
│   │   └── limiter.py               # Rate limiting
│   ├── charx/
│   │   ├── parser.py                # charx ZIP 파싱 (JPEG offset 처리)
│   │   ├── lorebook.py              # 로어북 키워드 인덱스 & 매칭 엔진
│   │   └── schemas.py               # chara_card_v3 Pydantic 모델
│   ├── memory/
│   │   ├── base.py                  # MemoryBackend 프로토콜 (추상 인터페이스)
│   │   ├── mem0_backend.py          # InstrumentedMemory (mem0 + OTel 래퍼)
│   │   └── factory.py               # 백엔드 팩토리 (설정 기반 인스턴스 생성)
│   ├── providers/
│   │   ├── base.py                  # LLM Provider 추상 인터페이스
│   │   ├── openai.py                # OpenAI provider
│   │   ├── google.py                # Google Gemini provider
│   │   └── anthropic.py             # Anthropic Claude provider
│   ├── services/
│   │   ├── context_builder.py       # 로어북 + 메모리 + 히스토리 → 컨텍스트 조합
│   │   ├── curator.py               # 경량 Curator — N턴마다 서사 요약 합성
│   │   ├── system_stabilizer.py     # canonical system prompt 보호 (BP1)
│   │   ├── window_recovery.py       # RisuAI 윈도우 이동 감지 & 복구
│   │   ├── message_compressor.py    # LLM-free 턴 압축
│   │   ├── post_turn.py             # 비동기 후처리 (mem0 add + curator 트리거)
│   │   ├── llm.py                   # LLM 라우팅 서비스
│   │   └── database.py              # DB 서비스
│   ├── schemas/
│   │   ├── chat.py                  # OpenAI 호환 요청/응답 스키마
│   │   └── memory.py                # 메모리 스키마
│   ├── models/                      # SQLModel ORM 모델
│   └── main.py                      # 앱 진입점
├── characters/                      # charx 파일 저장소 (업로드된 캐릭터팩)
├── evals/                           # LLM 평가 프레임워크
├── grafana/                         # Grafana 대시보드 JSON
├── prometheus/                      # Prometheus 설정
├── scripts/                         # 유틸 스크립트
├── logs/                            # JSONL 로그 출력
├── docker-compose.yml
├── Dockerfile
├── Makefile
├── pyproject.toml
├── CLAUDE.md
└── SAGA_MEM0.md                     # 컨텍스트 빌드 아키텍처 설계서
```

## Key Dependencies

```
fastapi, uvicorn, uvloop           # 웹 프레임워크
structlog                           # 구조화 로깅
opentelemetry-sdk                   # 분산 트레이싱
opentelemetry-instrumentation-fastapi
langfuse                            # LLM observability
prometheus-client, starlette-prometheus  # 메트릭
mem0ai                              # 장기 메모리
qdrant-client                       # 벡터 스토어 (mem0 백엔드)
tenacity                            # 재시도
slowapi                             # Rate limiting
pydantic, pydantic-settings         # 데이터 검증/설정
httpx                               # async HTTP 클라이언트
rich                                # 콘솔 출력
```

## charx Format (RisuAI Character Pack)

charx는 ZIP 아카이브 (앞에 JPEG 썸네일이 prepend됨):
```
*.charx
├── card.json              # chara_card_v3 스펙 — 핵심 캐릭터 정의
│   ├── data.name          # 캐릭터/모듈 이름
│   ├── data.description   # 세계관/설정 설명
│   ├── data.first_mes     # 첫 인사 메시지
│   ├── data.alternate_greetings[]  # 대체 시나리오 인사들
│   ├── data.character_book.entries[]  # 로어북 엔트리
│   │   ├── keys[]         # 트리거 키워드 (빈 배열 = constant 항상 주입)
│   │   ├── content        # 주입할 컨텍스트 텍스트
│   │   ├── constant       # true면 항상 주입, false면 키워드 매칭 시만
│   │   ├── insertion_order # 주입 우선순위 (낮을수록 먼저)
│   │   └── enabled        # 활성화 여부
│   └── data.extensions.risuai  # RisuAI 전용 확장 (SD 데이터, 뷰스크린 등)
├── x_meta/1.json          # 이미지 생성 파라미터 (SD 메타데이터)
├── assets/icon/           # 캐릭터 아이콘 이미지
└── module.risum           # 바이너리 모듈 데이터 (RisuAI 전용 압축 포맷)
```

### 로어북 엔트리 유형
- **constant (항상 주입)**: keys가 빈 배열, `constant: true` — 세계관 설정, 조직 정보, 관계도
- **keyword-triggered**: keys에 캐릭터명/장소명 등, `constant: false` — 대화에서 키워드 감지 시 주입
- **insertion_order**: 100번대=세계관, 200번대=조직, 300번대=캐릭터, 400번대=NPC/몹, 500번대=관계

### charx 파서 요구사항
- ZIP 압축 해제 시 앞의 JPEG 바이트를 건너뛰어야 함 (offset 탐색 필요)
- card.json 파싱 → Pydantic 모델로 검증
- 로어북 엔트리를 키워드 인덱스로 구축 (빠른 매칭용)
- `{{user}}` 플레이스홀더를 실제 유저명으로 치환

## RisuAI Integration

- RisuAI는 OpenAI API 포맷(`/v1/chat/completions`)을 사용
- 프록시는 이 엔드포인트를 구현하고, 내부에서 multi-provider로 라우팅
- streaming(SSE) 응답 지원 필수
- charx에서 파싱된 캐릭터 정의 + 로어북을 컨텍스트에 주입
- 대화 메시지에서 키워드 매칭 → 해당 로어북 엔트리 동적 주입
- constant 엔트리는 매 요청마다 자동 포함

## Memory System (mem0 + 경량 Curator)

> 상세 설계: [SAGA_MEM0.md](./SAGA_MEM0.md) 섹션 3, 9 참조

### 메모리 백엔드 프로토콜

```python
# app/memory/base.py — 메모리 백엔드 프로토콜
class MemoryBackend(Protocol):
    async def add(self, messages: list[dict], user_id: str, *, agent_id: str | None = None, app_id: str | None = None, metadata: dict | None = None) -> dict: ...
    async def search(self, query: str, user_id: str, *, agent_id: str | None = None, limit: int = 5, filters: dict | None = None) -> dict: ...
    async def get(self, user_id: str, *, agent_id: str | None = None) -> dict: ...
    async def delete(self, memory_id: str) -> bool: ...
    async def update(self, memory_id: str, data: str) -> dict: ...
```

### 구현 백엔드
- **mem0**: `app/memory/mem0_backend.py` — InstrumentedMemory (mem0 + OTel 래퍼)
- **향후 확장**: Zep, Letta, MemGPT, 커스텀 RAG 등 동일 인터페이스로 교체 가능

### mem0 스코핑 (4차원)
- `user_id` = 플레이어, `agent_id` = charx 캐릭터 페르소나, `run_id` = RP 세션, `app_id` = charx 모듈
- `agent_id` 생략 시 = 공유 세계관 메모리 (모든 캐릭터 참조 가능)

### 2계층 메모리 전략
- **Fact 레벨 (매 턴)**: mem0 add()가 자동으로 fact 추출 → ADD/UPDATE/DELETE 큐레이팅
- **서사 레벨 (N턴마다)**: 경량 Curator가 LLM으로 서사 요약 합성 → mem0에 `metadata.type="narrative_summary"`로 저장

### 컨텍스트 빌드 흐름 (요약)
1. 서사 요약 검색 (narrative_summary) — curator가 N턴마다 갱신
2. charx 로어북에서 constant 엔트리 수집
3. 대화 메시지에서 키워드 매칭 → triggered 로어북 엔트리 추가
4. mem0.search()로 캐릭터별 기억 + 공유 기억 검색
5. Greedy Token Budget Allocator로 우선순위 기반 조합
6. LLM 응답 후 → 비동기로 mem0.add() + curator 트리거

## Observability Stack

### Logging (structlog)
- 개발: 컬러 콘솔 출력 (ConsoleRenderer)
- 프로덕션: JSON 포맷 (JSONRenderer)
- 일별 JSONL 파일 로깅
- 컨텍스트 변수: request_id, session_id, user_id, model

### Tracing (OpenTelemetry + Langfuse)
- FastAPI 자동 계측 (opentelemetry-instrumentation-fastapi)
- LLM 호출별 span: model, tokens, latency, cost
- Langfuse trace로 대화 흐름 전체 추적
- 커스텀 span으로 메모리 검색/저장 시간 측정

### Metrics (Prometheus + Grafana)
- `http_requests_total` — 요청 수 (method, endpoint, status)
- `http_request_duration_seconds` — 요청 지연 시간
- `llm_inference_duration_seconds` — LLM 추론 시간 (model별)
- `llm_tokens_total` — 토큰 사용량 (model, type=input/output)
- `memory_operation_duration_seconds` — 메모리 백엔드 작업 시간 (backend, op=add/search/get)
- `lorebook_entries_injected` — 로어북 엔트리 주입 수 (type=constant/triggered)
- `context_build_duration_seconds` — 컨텍스트 빌드 전체 소요 시간

## Environment Configuration

```bash
# Application
APP_ENV=development          # development | staging | production

# LLM Providers
OPENAI_API_KEY=
GOOGLE_API_KEY=
ANTHROPIC_API_KEY=
DEFAULT_MODEL=gpt-4o-mini

# Memory (mem0 + Qdrant)
MEMORY_BACKEND=mem0               # mem0 | (향후: zep, letta, custom)
MEM0_COLLECTION_NAME=yangban_memories
MEM0_EMBEDDER_MODEL=text-embedding-3-small
QDRANT_PATH=./qdrant_db           # 로컬 임베디드 (개발)
# QDRANT_URL=http://localhost:6333  # 원격 Qdrant (프로덕션)

# Curator
CURATOR_INTERVAL=10               # N턴마다 서사 요약 갱신
CURATOR_MODEL=gpt-4o-mini         # 요약 전용 모델

# Characters
CHARX_STORAGE_DIR=./characters    # charx 파일 저장 경로

# Observability
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com

# Database (production)
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=yangban
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
```

## 10 Commandments

1. 모든 라우트에 rate limiting 데코레이터
2. 모든 LLM 호출에 Langfuse 트레이싱
3. 모든 비동기 작업에 적절한 에러 핸들링
4. 모든 로그는 structlog + lowercase_underscore 이벤트명
5. 모든 재시도는 tenacity 사용
6. 모든 콘솔 출력에 rich 포매팅
7. 성공 응답만 캐싱
8. 모든 import는 파일 최상단
9. 모든 DB 작업은 async
10. 모든 엔드포인트에 type hints + Pydantic 모델

## Common Pitfalls

- structlog 이벤트에 f-string 사용
- 함수 내부에 import 추가
- 라우트에 rate limiting 누락
- LLM 호출에 Langfuse 트레이싱 누락
- 에러 응답 캐싱
- `logger.error()` 대신 `logger.exception()` 미사용
- async 없이 blocking I/O 호출
- 하드코딩된 시크릿/API 키
- 함수 시그니처에 type hints 누락
- OpenTelemetry span 닫기 누락

## References

- RisuAI: https://github.com/kwaroran/RisuAI
- mem0ai: https://docs.mem0.ai/
- structlog: https://www.structlog.org/
- OpenTelemetry Python: https://opentelemetry.io/docs/languages/python/
- Langfuse: https://langfuse.com/docs
- FastAPI: https://fastapi.tiangolo.com/
- Prometheus: https://prometheus.io/docs/
- Grafana: https://grafana.com/docs/
