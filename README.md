# Yang-Ban

RisuAI charx 캐릭터/모듈 기반 RP 프록시 서버.
charx 로어북 주입 + mem0 장기 메모리 + 구조화 로깅/트레이싱/메트릭을 학습하며 구축하는 프로젝트.

```
RisuAI Client ──POST /v1/chat/completions──▶ Yang-Ban Proxy ──▶ OpenAI / Gemini / Claude
                                                │
                                                ├─ charx 로어북 주입
                                                ├─ mem0 메모리 검색/저장
                                                ├─ 서사 요약 (Curator)
                                                └─ Observability (structlog + OTel + Prometheus)
```

## Quick Start

```bash
# 의존성 설치
uv sync

# 환경 변수 설정
cp .env.example .env
# .env에 API 키 입력 (OPENAI_API_KEY 필수 — mem0 임베딩에 사용)

# 서버 실행
uv run uvicorn app.main:app --reload --port 8000

# 확인
open http://localhost:8000/docs        # Swagger UI
curl http://localhost:8000/metrics/    # Prometheus 메트릭
```

## 주요 기능

### charx 캐릭터팩 처리

RisuAI Realm에서 배포되는 `.charx` 파일(JPEG+ZIP 아카이브)을 파싱하여 캐릭터 설정과 로어북을 추출한다.

- **constant 엔트리**: 세계관, 조직 정보 — 매 요청마다 자동 주입
- **triggered 엔트리**: 대화에서 키워드 감지 시 동적 주입 (예: "루비아" 언급 → 해당 캐릭터 정보 주입)

```bash
# 캐릭터 업로드
curl -X POST http://localhost:8000/v1/characters/upload \
  -F "file=@캐릭터.charx"

# 캐릭터 목록
curl http://localhost:8000/v1/characters
```

### mem0 메모리 시스템

캐릭터별 격리된 장기 메모리. 대화에서 중요 사실을 자동 추출하고 저장한다.

| 스코프 | 역할 | 예시 |
|--------|------|------|
| `user_id` | 플레이어 | `"player_yang"` |
| `agent_id` | 캐릭터 페르소나 | `"루비아"`, `"트릭시"` |
| `app_id` | charx 모듈 | `"현대던전시뮬"` |

**2계층 메모리**:
- **Fact 레벨** (매 턴): mem0가 대화에서 fact 추출 → ADD/UPDATE/DELETE 자동 큐레이팅
- **서사 레벨** (10턴마다): Curator가 LLM으로 서사 요약 합성 → 컨텍스트 최우선 주입

### 컨텍스트 빌드 파이프라인

요청마다 여러 소스를 병렬 검색하고, 토큰 예산 내에서 우선순위 기반으로 조립한다.

```
asyncio.gather (4개 병렬)
  ├─ narrative_summary 검색
  ├─ keyword-triggered 로어북 매칭
  ├─ mem0 캐릭터별 기억 검색
  └─ mem0 공유 세계관 기억 검색

Greedy Token Budget Assembly (우선순위):
  ⓪ [서사 요약]     ← Curator가 N턴마다 갱신
  ① [세계관 설정]    ← charx constant 로어북
  ② [캐릭터 정보]    ← triggered 로어북
  ③ [기억]          ← 캐릭터별 mem0 검색
  ④ [세계 이벤트]    ← 공유 mem0 검색
```

### Multi-Provider LLM 라우팅

model 이름 prefix로 자동 라우팅:

| Prefix | Provider | 특이사항 |
|--------|----------|----------|
| `gpt-*`, `o1-*` | OpenAI | system msg 병합, `max_completion_tokens` |
| `gemini-*` | Google | system_instruction 변환, 429 retry |
| `claude-*` | Anthropic | 3-Breakpoint prompt caching |

### Observability

| 계층 | 도구 | 출력 |
|------|------|------|
| 로깅 | structlog | 구조화 JSONL (dev: 컬러 콘솔) |
| 트레이싱 | OpenTelemetry | Langfuse (LLM 호출 추적) |
| 메트릭 | prometheus-client | `/metrics` 엔드포인트 → Grafana |
| 재시도 | tenacity | 지수 백오프 (429/5xx) |

## API

### Chat (RisuAI 호환)

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "x-session-id: session-001" \
  -H "x-user-id: player_yang" \
  -H "x-agent-id: 루비아" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [
      {"role": "system", "content": "당신은 루비아입니다."},
      {"role": "user", "content": "오늘 던전 갈래?"}
    ],
    "stream": false
  }'
```

### Memory

```bash
# 기억 추가
curl -X POST http://localhost:8000/v1/memory \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "루비아 생일은 3월 15일"}], "user_id": "player_yang", "agent_id": "루비아"}'

# 기억 검색
curl -X POST http://localhost:8000/v1/memory/search \
  -H "Content-Type: application/json" \
  -d '{"query": "루비아 생일", "user_id": "player_yang", "agent_id": "루비아"}'

# 기억 삭제
curl -X DELETE http://localhost:8000/v1/memory/{memory_id}
```

## 프로젝트 구조

```
app/
├── api/v1/
│   ├── chat.py              POST /v1/chat/completions (Phase A~G 파이프라인)
│   ├── characters.py        charx 업로드/목록
│   ├── memory.py            메모리 CRUD
│   └── router.py            라우터 집합
├── core/
│   ├── config.py            Pydantic Settings (.env 로딩)
│   ├── logging.py           structlog 설정
│   ├── tracing.py           OTel + Langfuse 설정
│   ├── metrics.py           Prometheus 메트릭 정의
│   ├── middleware.py         request_id + OTel span + 메트릭 미들웨어
│   └── limiter.py           Rate limiting (slowapi)
├── charx/
│   ├── parser.py            charx ZIP 파싱 (JPEG offset 탐색)
│   ├── lorebook.py          키워드 인덱스 + 매칭 엔진
│   └── schemas.py           chara_card_v3 Pydantic 모델
├── memory/
│   ├── base.py              MemoryBackend Protocol
│   ├── mem0_backend.py      InstrumentedMemory (OTel + Prometheus + tenacity)
│   └── factory.py           설정 기반 백엔드 팩토리
├── providers/
│   ├── base.py              LLMProvider Protocol
│   ├── openai.py            OpenAI (httpx)
│   ├── google.py            Google Gemini (httpx)
│   └── anthropic.py         Anthropic Claude (3-BP cache)
├── services/
│   ├── context_builder.py   로어북 + 메모리 → Greedy Token Assembly
│   ├── curator.py           N턴마다 서사 요약 합성 → mem0 저장
│   ├── system_stabilizer.py canonical system prompt 보호
│   ├── window_recovery.py   RisuAI 윈도우 이동 감지 & 복구
│   ├── message_compressor.py LLM-free 턴 압축
│   ├── post_turn.py         비동기 후처리 (mem0 add + curator)
│   ├── llm.py               model → provider 라우팅
│   └── database.py          SQLite async (session KV + turn log)
├── schemas/
│   ├── chat.py              OpenAI 호환 요청/응답
│   └── memory.py            메모리 스키마
└── main.py                  FastAPI app (lifespan + middleware + routes)
```

## 환경 변수

```bash
# 필수
OPENAI_API_KEY=sk-...              # mem0 임베딩 + OpenAI provider

# 선택 (사용할 provider만)
GOOGLE_API_KEY=
ANTHROPIC_API_KEY=

# 메모리
QDRANT_PATH=./qdrant_db            # 벡터 스토어 경로
CURATOR_INTERVAL=10                # 서사 요약 갱신 주기 (턴)
CURATOR_MODEL=gpt-4o-mini          # 요약 전용 모델

# Observability
LANGFUSE_PUBLIC_KEY=               # 트레이싱 (선택)
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
```

## 설계 문서

- **[CLAUDE.md](./CLAUDE.md)** — 프로젝트 규칙, 코딩 표준, 10 Commandments
- **[SAGA_MEM0.md](./SAGA_MEM0.md)** — 컨텍스트 빌드 파이프라인 아키텍처 (13개 섹션)
  - RISU_ENE(SAGA v3.0) 패턴을 mem0 기반으로 재설계한 상세 설계서

## Tech Stack

| 영역 | 도구 |
|------|------|
| Runtime | Python 3.13+, FastAPI, uvicorn, uvloop |
| Memory | mem0ai, Qdrant (벡터), SQLite (앱 DB) |
| LLM | OpenAI, Google Gemini, Anthropic Claude |
| Logging | structlog |
| Tracing | OpenTelemetry SDK, Langfuse |
| Metrics | prometheus-client |
| Retry | tenacity |
| Rate Limit | slowapi |
| HTTP | httpx (async) |

## License

MIT
