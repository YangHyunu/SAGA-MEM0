# SAGA_MEM0: Yang-Ban Context Build Architecture

> RISU_ENE(SAGA v3.0)의 컨텍스트 파이프라인을 mem0 기반으로 재설계한 아키텍처 문서.
> RISU_ENE의 .md 캐시 + ChromaDB + Letta 구조를 mem0 단일 메모리 레이어로 단순화하되,
> 검증된 패턴(greedy token allocator, 3-BP cache, SystemStabilizer)은 그대로 포팅한다.

---

## 1. 전체 흐름

```
RisuAI 클라이언트
    │
    │  POST /v1/chat/completions (OpenAI 포맷)
    ▼
┌──────────────────────────────────────────────────────────┐
│  FastAPI Proxy (Yang-Ban)                                │
│                                                          │
│  Phase 0  미들웨어 (request_id, structlog, OTel, Langfuse)│
│  Phase 1  정적 컨텍스트 수집 (charx constant 로어북)      │
│  Phase 2  동적 컨텍스트 수집 (키워드 매칭 + mem0 검색)    │
│  Phase 3  토큰 예산 기반 Greedy Assembly                  │
│  Phase 4  최종 메시지 조립 (3-BP cache 적용)              │
│  Phase 5  LLM 라우팅 & 응답 반환                         │
│  Phase 6  비동기 후처리 (mem0 add, 메트릭)                │
│                                                          │
└─────────────────┬────────────────────────────────────────┘
                  │
        ┌─────────┼─────────┐
        ▼         ▼         ▼
      OpenAI   Gemini   Anthropic
```

---

## 2. RISU_ENE → Yang-Ban 맵핑

### 2.1 아키텍처 비교

| RISU_ENE (SAGA v3.0) | Yang-Ban (SAGA_MEM0) | 변경 이유 |
|----------------------|----------------------|-----------|
| stable_prefix.md (세계관/서사요약) | mem0 narrative_summary + charx 인메모리 캐시 | 경량 curator가 N턴마다 서사 요약을 mem0에 저장 |
| live_state.md (매 턴 상태) | mem0.search() 결과로 대체 | mem0가 상태 기억을 자체 관리 |
| ChromaDB 에피소드 (RRF 3-source) | mem0 vector search (agent_id별) | mem0가 벡터 검색 담당, 초기엔 단일 검색 |
| SQLite active lore | charx keyword-triggered 로어북 | 키워드 매칭 엔진은 동일 |
| Letta curator (10턴마다) | 경량 curator (N턴마다 서사 요약 갱신) | mem0 fact 큐레이팅 + 별도 서사 합성 |
| MessageCompressor (LLM-free) | 포팅 — turn_log 기반 요약 재조합 | 검증된 패턴, 비용 0 |
| SystemStabilizer (BP1 보호) | 포팅 — charx system prompt 고정 | RisuAI가 동일하게 system msg 변경 |
| 3-BP cache_control | 포팅 — Anthropic 모델 시 적용 | 비용 절감 핵심 |
| WindowRecovery (해시 감지) | 포팅 — RisuAI 윈도우 이동 감지 | 동일 문제 존재 |
| Flash 추출 (Sub-B extractors.py) | 제거 — mem0 fact extraction이 대체 | 이중 추출 불필요 |
| NPC 레지스트리 + LLM 디딥 | 향후 — Graph Memory로 대체 | enable_graph=True 활성화 시 |
| Cache Warming (4.5분 핑) | 향후 | 초기엔 불필요 |

### 2.2 단순화 결정

RISU_ENE는 6개월간 진화하며 복잡해진 구조. Yang-Ban은 핵심만 가져간다:

- **제거**: .md 캐시 2-tier, Letta Memory Block, ChromaDB, NPC 레지스트리
- **포팅**: Greedy Token Allocator, 3-BP Cache, SystemStabilizer, WindowRecovery, MessageCompressor
- **대체**: mem0가 에피소드 저장/검색/fact 큐레이팅을 통합 담당
- **추가**: 경량 curator — N턴마다 서사 요약을 LLM으로 합성하여 mem0에 저장 (stable_prefix.md 대체)

---

## 3. mem0 메모리 스코핑

### 3.1 4차원 스코핑

| mem0 파라미터 | Yang-Ban 맵핑 | 예시 |
|--------------|---------------|------|
| `user_id` | 플레이어 (RisuAI 사용자) | `"player_yang"` |
| `agent_id` | charx 캐릭터 페르소나 | `"루비아"`, `"트릭시"`, `"최은지"` |
| `run_id` | 개별 RP 세션 | `"session-20260401"` |
| `app_id` | charx 모듈 (시나리오) | `"현대던전시뮬"` |

### 3.2 메모리 분리 전략

```python
# 캐릭터별 기억 — agent_id로 격리
await memory.add(
    messages=[{"role": "user", "content": "루비아 생일 선물로 게임기 줬어"}],
    user_id="player_yang",
    agent_id="루비아",
    app_id="현대던전시뮬",
)

# 세계관 공유 기억 — agent_id 생략
await memory.add(
    messages=[{"role": "system", "content": "던전이 C등급에서 B등급으로 승격됨"}],
    user_id="player_yang",
    app_id="현대던전시뮬",
    # agent_id 생략 = 모든 캐릭터가 참조 가능
)

# 캐릭터 관점에서 검색
results = await memory.search(
    query="던전 코어",
    user_id="player_yang",
    agent_id="루비아",  # 루비아가 아는 것만
)
```

### 3.3 charx → agent_id 추출

charx의 `character_book.entries[]`에서 캐릭터를 식별하여 agent_id로 등록:

| 캐릭터 | agent_id | 메모리 스코프 | 식별 기준 |
|--------|----------|--------------|-----------|
| 루비아 | `rubia` | player + rubia 간 기억 | `keys: ["루비아", "Rubia"]` |
| 트릭시 | `trixie` | player + trixie 간 기억 | `keys: ["트릭시", "Trixie"]` |
| 최은지 | `choi_eunji` | player + choi_eunji 간 기억 | `keys: ["최은지"]` |
| (공유) | `None` | 모든 캐릭터 참조 가능 | constant 엔트리 중 세계관 이벤트 |

---

## 4. 컨텍스트 빌드 파이프라인 (Phase별 상세)

### Phase 0: 미들웨어

```
요청 수신
 ├─ request_id = uuid4()
 ├─ structlog.bind(request_id=request_id, session_id=..., user_id=...)
 ├─ OTel: tracer.start_span("chat_completion")
 └─ Langfuse: trace 시작
```

### Phase 1: 정적 컨텍스트 수집 (캐시 가능)

charx card.json에서 파싱된 데이터 중 **느리게 변하는 것**:

```
charx 인메모리 캐시에서 읽기 (charx 업로드 시 파싱 & 캐싱)
 │
 ├─ character.name          → 캐릭터 이름
 ├─ character.description   → 세계관/설정 설명
 ├─ character.first_mes     → 첫 인사 메시지
 │
 └─ constant 로어북 수집
      └─ character_book.entries[] 중:
           - constant == true
           - enabled == true
           - keys == [] (빈 배열)
      └─ insertion_order 오름차순 정렬
      └─ 100번대=세계관, 200번대=조직, 300번대=캐릭터 기본
```

**캐싱 전략**: charx 파일이 변경되지 않는 한 인메모리에 유지. `dict[charx_id, ParsedCharacter]`.

### Phase 2: 동적 컨텍스트 수집 (asyncio.gather 병렬)

```python
# 4개 작업을 병렬 실행
narrative, triggered, char_memories, shared_memories = await asyncio.gather(
    memory.search(                                          # [2-pre] 서사 요약
        "narrative summary",
        user_id=user_id,
        agent_id=active_char,
        filters={"metadata": {"type": "narrative_summary"}},
        limit=1,
    ),
    match_triggered_lorebook(messages, lorebook_index),     # [2a]
    memory.search(query, user_id, agent_id=active_char),    # [2b]
    memory.search(query, user_id, agent_id=None),            # [2c]
    return_exceptions=True,  # 하나 실패해도 나머지 사용
)
```

#### [2a] 키워드 매칭 → triggered 로어북

```
messages[-3:] (최근 3턴)에서 텍스트 추출
    │
    ├─ 로어북 인덱스에서 키워드 스캔
    │    └─ keys: ["루비아", "Rubia"] → content 매칭
    │    └─ 대소문자 무시, 한/영 모두 체크
    │
    ├─ 매칭된 entries를 insertion_order 오름차순 정렬
    │
    └─ 중복 제거 (constant와 겹치는 entry 제외)
```

#### [2b] mem0 캐릭터 기억 검색

```python
char_memories = await memory.search(
    query=messages[-1]["content"],  # 최근 user 메시지
    user_id=player_id,
    agent_id=active_character_id,   # 현재 활성 캐릭터
    limit=10,
)
# → 해당 캐릭터가 플레이어에 대해 기억하는 것
```

#### [2c] mem0 공유 세계관 기억 검색

```python
shared_memories = await memory.search(
    query=messages[-1]["content"],
    user_id=player_id,
    # agent_id 생략 → 공유 메모리에서 검색
    limit=5,
)
# → 세계관 이벤트, 모든 캐릭터가 아는 사실
```

### Phase 3: Greedy Token Budget Assembly

RISU_ENE의 `_assemble_dynamic()` 패턴을 포팅. 우선순위 순으로 토큰 예산을 소비한다.

```
budget = provider_context_limit - system_tokens - history_tokens - response_reserve
                                                                    (보통 4096)

우선순위 (높은순):
┌────┬──────────────────────┬────────────────────────────────┬──────────┐
│ #  │ 소스                 │ 내용                           │ 예산 cap │
├────┼──────────────────────┼────────────────────────────────┼──────────┤
│ ⓪  │ narrative_summary    │ curator가 합성한 서사 요약      │ 1500자   │
│ ①  │ constant 로어북      │ 세계관, 조직, 기본 캐릭터 설정  │ 없음     │
│ ②  │ triggered 로어북     │ 키워드 매칭된 캐릭터/장소 정보  │ 800자/건 │
│ ③  │ mem0 캐릭터 기억     │ agent_id별 검색 결과           │ 500자/건 │
│ ④  │ mem0 공유 기억       │ 세계관 이벤트                  │ 500자/건 │
└────┴──────────────────────┴────────────────────────────────┴──────────┘

각 우선순위 그룹 처리 로직:
  1. 그룹 헤더 토큰 계산 (예: "[세계관 설정]")
  2. remaining -= header_tokens
  3. for entry in sorted(entries):
       tokens = count_tokens(entry.content[:cap])
       if tokens <= remaining:
           sections.append(entry)
           remaining -= tokens
       else:
           break  # 예산 초과 → 다음 그룹으로
  4. 아무것도 추가 안 됐으면 header_tokens 반환
```

#### 의사코드

```python
async def assemble_context(
    narrative_summary: str | None,
    constant_entries: list[LorebookEntry],
    triggered_entries: list[LorebookEntry],
    character_memories: list[MemoryResult],
    shared_memories: list[MemoryResult],
    token_budget: int,
) -> str:
    remaining = token_budget
    blocks: list[str] = []

    # ⓪ narrative_summary (서사 요약 — 최우선, curator가 N턴마다 갱신)
    if narrative_summary:
        summary_text = narrative_summary[:1500]  # 1500자 cap
        t = count_tokens(summary_text)
        if t <= remaining:
            blocks.append("[서사 요약]\n" + summary_text)
            remaining -= t

    # ① constant 로어북 (세계관 기반 — 항상 포함 시도)
    remaining, block = _greedy_fill(
        entries=[e.content for e in constant_entries],
        header="[세계관 설정]",
        remaining=remaining,
        per_entry_cap=None,  # constant는 cap 없음
    )
    if block:
        blocks.append(block)

    # ② triggered 로어북 (활성 캐릭터 정보)
    remaining, block = _greedy_fill(
        entries=[e.content for e in triggered_entries],
        header="[캐릭터 정보]",
        remaining=remaining,
        per_entry_cap=800,
    )
    if block:
        blocks.append(block)

    # ③ mem0 캐릭터 기억
    remaining, block = _greedy_fill(
        entries=[m["memory"] for m in character_memories],
        header="[기억]",
        remaining=remaining,
        per_entry_cap=500,
    )
    if block:
        blocks.append(block)

    # ④ mem0 공유 기억
    remaining, block = _greedy_fill(
        entries=[m["memory"] for m in shared_memories],
        header="[세계 이벤트]",
        remaining=remaining,
        per_entry_cap=500,
    )
    if block:
        blocks.append(block)

    return "\n\n".join(blocks)


def _greedy_fill(
    entries: list[str],
    header: str,
    remaining: int,
    per_entry_cap: int | None,
) -> tuple[int, str | None]:
    """RISU_ENE _assemble_dynamic 패턴 포팅.
    토큰 예산 내에서 greedy하게 엔트리를 채운다.
    """
    header_tokens = count_tokens(header)
    if header_tokens > remaining:
        return remaining, None

    remaining -= header_tokens
    lines: list[str] = []

    for entry in entries:
        text = entry[:per_entry_cap] if per_entry_cap else entry
        t = count_tokens(text)
        if t <= remaining:
            lines.append(text)
            remaining -= t
        # 예산 초과 시 해당 엔트리 스킵 (다음 엔트리는 더 짧을 수 있음)

    if not lines:
        remaining += header_tokens  # 헤더 토큰 반환
        return remaining, None

    return remaining, header + "\n" + "\n".join(lines)
```

### Phase 4: 최종 메시지 조립

#### 4.1 기본 구조

```
messages[] 배열:
[0]   system: canonical_system_prompt (charx description + 기본 설정)
[1]   user:   "[요약 1: Turn 1-8]"              ← MessageCompressor chunk (있을 때)
[2]   assistant: "Turn 1: ... Turn 2: ..."       ← immutable chunk
...
[N-2] assistant: "이전 턴의 응답"
[N-1] user: context_block + "\n\n" + 원본_유저_메시지   ← 동적 컨텍스트 주입
```

#### 4.2 Anthropic 3-Breakpoint Cache (포팅)

Anthropic 모델 사용 시 `cache_control: {"type": "ephemeral"}` 적용:

```
BP1: messages[0] (system prompt)
     └─ SystemStabilizer가 매 턴 동일함을 보장
     └─ charx가 바뀌지 않는 한 캐시 적중

BP2: 대화 히스토리 중간점의 assistant 메시지
     └─ MessageCompressor의 마지막 summary chunk (있으면)
     └─ 없으면 전체 assistant 메시지의 중간 인덱스

BP3: 마지막 assistant 메시지
     └─ 직전 턴까지의 응답 — 현재 턴 완료 전까지 불변

Dynamic: messages[N-1] (마지막 user 메시지)
     └─ context_block이 prepend됨
     └─ 매 턴 변하므로 캐시 밖에 위치
```

#### 4.3 Non-Anthropic 모델 (OpenAI, Gemini)

```
cache_control 없음.
context_block을 system 메시지 끝에 append.
(Anthropic처럼 user 메시지에 넣으면 혼란 가능성)
```

#### 4.4 SystemStabilizer (포팅)

RisuAI는 로어북 엔트리가 활성화될 때 system 메시지 내용을 변경한다.
이를 감지하여 canonical system prompt를 보호:

```
첫 턴:
  canonical = system_message
  hash = md5(canonical)
  → DB에 저장

이후 턴:
  new_hash = md5(system_message)
  if new_hash == canonical_hash:
      pass  # 변경 없음
  elif jaccard_similarity(canonical, system_message) < 0.30:
      canonical = system_message  # 완전히 다른 캐릭터 → 교체
  else:
      delta = diff(canonical, system_message)  # paragraph-level diff
      → delta를 dynamic context로 이동 (캐시 밖)
      → canonical은 유지 (BP1 보호)
```

### Phase 5: LLM 라우팅 & 응답 반환

```
model 파라미터에서 provider 결정:
  "gpt-*"     → OpenAI provider
  "gemini-*"  → Google provider
  "claude-*"  → Anthropic provider

stream == true:
  SSE (Server-Sent Events) 응답
  각 chunk에 OTel event 기록

stream == false:
  JSON 응답
  전체 응답에 OTel attributes 기록
```

### Phase 6: 비동기 후처리

유저는 Phase 5에서 이미 응답을 받았다. Phase 6는 `asyncio.create_task`로 백그라운드 실행.

```python
async def post_turn_process(
    messages: list[dict],
    user_id: str,
    agent_id: str,
    app_id: str,
):
    """응답 반환 후 비동기 실행. 유저 대기 없음."""

    # 1. mem0에 대화 기억 저장
    #    → 내부에서 LLM 2회 호출 (fact extraction + ADD/UPDATE/DELETE 판단)
    with tracer.start_as_current_span("memory.add") as span:
        result = await memory.add(
            messages=[
                messages[-2],  # user 메시지
                messages[-1],  # assistant 응답
            ],
            user_id=user_id,
            agent_id=agent_id,
            app_id=app_id,
        )
        span.set_attribute("mem0.events_count", len(result.get("results", [])))

    # 2. 세계관 변화 감지 시 공유 메모리에도 저장
    world_events = [
        r for r in result.get("results", [])
        if "세계" in r.get("memory", "") or "승급" in r.get("memory", "")
    ]
    if world_events:
        await memory.add(
            messages=[{"role": "system", "content": "; ".join(
                e["memory"] for e in world_events
            )}],
            user_id=user_id,
            app_id=app_id,
            # agent_id 생략 → 공유
        )

    # 3. 경량 Curator — N턴마다 서사 요약 갱신
    turn_number = await db.get_turn_count(session_id)
    if turn_number % CURATOR_INTERVAL == 0:  # 기본 10턴
        await curate_narrative_summary(
            user_id=user_id,
            agent_id=agent_id,
            app_id=app_id,
            recent_messages=messages[-CURATOR_INTERVAL * 2:],
        )

    # 4. 메트릭 기록
    memory_op_duration.observe(elapsed)
    lorebook_entries_injected.inc(injected_count)

    # 5. structlog 기록
    logger.info(
        "post_turn_completed",
        user_id=user_id,
        agent_id=agent_id,
        mem0_events=len(result.get("results", [])),
        curator_triggered=(turn_number % CURATOR_INTERVAL == 0),
    )
```

---

## 5. mem0 설정

### 5.1 기본 설정 (개발)

```python
MEM0_CONFIG = {
    "llm": {
        "provider": "openai",
        "config": {
            "model": "gpt-4o-mini",
            "temperature": 0.1,
            "max_tokens": 2000,
        },
    },
    "embedder": {
        "provider": "openai",
        "config": {
            "model": "text-embedding-3-small",
        },
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "collection_name": "yangban_memories",
            "path": "./qdrant_db",  # 로컬 임베디드
        },
    },
    "custom_fact_extraction_prompt": RP_FACT_EXTRACTION_PROMPT,
    "version": "v1.1",
}

memory = AsyncMemory.from_config(config_dict=MEM0_CONFIG)
```

### 5.2 RP 전용 Fact Extraction 프롬프트

```python
RP_FACT_EXTRACTION_PROMPT = """
롤플레이 대화에서 다음 유형의 사실만 추출하세요:

1. 캐릭터 감정/태도 변화
   예: "루비아가 플레이어에게 호감을 보임"
2. 관계 변화
   예: "플레이어와 트릭시가 동맹을 맺음"
3. 중요 결정/행동
   예: "플레이어가 던전 코어를 파괴하기로 결정함"
4. 세계관 변화
   예: "C등급 던전이 B등급으로 승격됨"
5. 플레이어 선호/성격
   예: "플레이어는 비폭력적 해결을 선호함"

추출하지 말 것:
- 일상 대화, 인사, 잡담
- 전투 세부사항 (HP 변화, 스킬 사용 등 일시적 상태)
- 이미 로어북에 정의된 기존 설정 반복
- 메타 대화 (시스템 메시지, OOC)

{"facts": ["사실1", "사실2", ...]}
"""
```

### 5.3 Graph Memory 설정 (향후 — 캐릭터 관계 자동 추출)

```python
# enable_graph=True 활성화 시 캐릭터 관계가 자동 추출됨
MEM0_CONFIG_WITH_GRAPH = {
    **MEM0_CONFIG,
    "graph_store": {
        "provider": "neo4j",
        "config": {
            "url": "neo4j+s://xxxx.databases.neo4j.io",
            "username": "neo4j",
            "password": "${NEO4J_PASSWORD}",
        },
    },
}

# 또는 로컬 개발용 Kuzu
MEM0_CONFIG_WITH_GRAPH_DEV = {
    **MEM0_CONFIG,
    "graph_store": {
        "provider": "kuzu",
        "config": {"database_path": "./graph_db"},
    },
}

# 관계 포함 검색 결과:
# {
#   "results": [...],
#   "relations": [
#     {"source": "루비아", "relation": "colleague", "target": "이오네"},
#     {"source": "플레이어", "relation": "ally", "target": "루비아"}
#   ]
# }
```

---

## 6. Observability 연동

### 6.1 트레이싱 구조

```
chat_completion (root span)
├── lorebook.constant_collect     attrs: entry_count
├── lorebook.keyword_match        attrs: keywords_found, entries_matched
├── memory.search.character       attrs: agent_id, query_length, hits, latency_ms
├── memory.search.shared          attrs: hits, latency_ms
├── context.assemble              attrs: token_budget, tokens_used, sections_included
├── system.stabilize              attrs: is_canonical, delta_size
├── llm.inference                 attrs: model, input_tokens, output_tokens, latency_ms
│   └── langfuse.generation       attrs: cost, model, completion_tokens
└── [async] memory.add            attrs: facts_extracted, events_added/updated/deleted
```

### 6.2 InstrumentedMemory 래퍼

mem0에는 내장 트레이싱이 없다. OTel span으로 감싸는 래퍼 클래스:

```python
class InstrumentedMemory:
    """mem0 AsyncMemory에 OTel + structlog + tenacity 재시도를 주입한 래퍼."""

    def __init__(self, config: dict):
        self._mem = AsyncMemory.from_config(config_dict=config)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def add(self, messages, user_id, **kwargs):
        with tracer.start_as_current_span("memory.add") as span:
            span.set_attributes({
                "mem0.operation": "add",
                "mem0.user_id": user_id,
                "mem0.agent_id": kwargs.get("agent_id", "shared"),
                "mem0.message_count": len(messages),
            })
            t0 = time.monotonic()
            result = await self._mem.add(messages, user_id=user_id, **kwargs)
            elapsed = time.monotonic() - t0

            events = result.get("results", [])
            span.set_attributes({
                "mem0.latency_ms": elapsed * 1000,
                "mem0.events_added": sum(1 for e in events if e.get("event") == "ADD"),
                "mem0.events_updated": sum(1 for e in events if e.get("event") == "UPDATE"),
                "mem0.events_deleted": sum(1 for e in events if e.get("event") == "DELETE"),
            })
            memory_op_duration.labels(backend="mem0", op="add").observe(elapsed)
            logger.info("memory_add_completed", user_id=user_id, events=len(events), duration=elapsed)
            return result

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def search(self, query, user_id, **kwargs):
        with tracer.start_as_current_span("memory.search") as span:
            span.set_attributes({
                "mem0.operation": "search",
                "mem0.user_id": user_id,
                "mem0.agent_id": kwargs.get("agent_id", "shared"),
                "mem0.query_length": len(query),
            })
            t0 = time.monotonic()
            result = await self._mem.search(query, user_id=user_id, **kwargs)
            elapsed = time.monotonic() - t0

            hits = len(result.get("results", []))
            span.set_attributes({"mem0.hits": hits, "mem0.latency_ms": elapsed * 1000})
            memory_op_duration.labels(backend="mem0", op="search").observe(elapsed)
            logger.info("memory_search_completed", user_id=user_id, hits=hits, duration=elapsed)
            return result
```

### 6.3 Prometheus 메트릭 (mem0 관련)

```python
from prometheus_client import Histogram, Counter

memory_op_duration = Histogram(
    "memory_operation_duration_seconds",
    "mem0 백엔드 작업 소요 시간",
    labelnames=["backend", "op"],  # op: add, search, get, delete
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)

memory_events_total = Counter(
    "memory_events_total",
    "mem0 이벤트 수",
    labelnames=["event_type"],  # ADD, UPDATE, DELETE, NONE
)

lorebook_entries_injected = Counter(
    "lorebook_entries_injected_total",
    "로어북 엔트리 주입 수",
    labelnames=["type"],  # constant, triggered
)
```

---

## 7. WindowRecovery (포팅)

RisuAI 클라이언트는 `max_token` 초과 시 대화 앞쪽을 잘라낸다.
Yang-Ban은 이를 감지하고 잃어버린 턴의 기억을 mem0에서 복구한다.

### 7.1 감지 메커니즘

```python
async def detect_window_shift(messages: list[dict], session_id: str) -> int:
    """첫 non-system 메시지의 해시를 비교하여 윈도우 이동 감지.
    
    Returns: 잃어버린 추정 턴 수 (0이면 이동 없음)
    """
    first_content = next(
        (m["content"] for m in messages if m["role"] != "system"),
        None,
    )
    if not first_content:
        return 0

    current_hash = hashlib.md5(first_content[:500].encode()).hexdigest()[:12]
    prev_hash = await db.get_kv(session_id, "window_first_msg_hash")

    if prev_hash and current_hash != prev_hash:
        # 윈도우 이동 감지
        total_turns = await db.get_turn_count(session_id)
        visible_turns = sum(1 for m in messages if m["role"] == "user")
        lost_turns = max(0, total_turns - visible_turns)
        return lost_turns

    await db.set_kv(session_id, "window_first_msg_hash", current_hash)
    return 0
```

### 7.2 복구 — mem0에서 잃어버린 기억 요약

```python
async def build_recovery_block(user_id: str, agent_id: str, lost_turns: int) -> str:
    """윈도우 이동으로 잃어버진 턴의 기억을 mem0에서 복구."""
    if lost_turns == 0:
        return ""

    # mem0에서 최근 기억 검색 (잃어버린 턴 수 기반)
    memories = await memory.search(
        query="최근 대화 요약",
        user_id=user_id,
        agent_id=agent_id,
        limit=min(lost_turns, 15),
    )

    if not memories.get("results"):
        return ""

    lines = [f"[이전 대화 기억 ({lost_turns}턴 분)]"]
    for m in memories["results"]:
        lines.append(f"- {m['memory']}")

    return "\n".join(lines)
```

---

## 8. MessageCompressor (포팅 — 단순화)

RISU_ENE의 LLM-free 압축을 포팅하되, turn_log 대신 mem0 history를 활용.

### 8.1 트리거 조건

```python
COMPRESS_THRESHOLD_RATIO = 0.50  # context_limit의 50% 초과 시
COMPRESS_TARGET_RATIO = 0.85     # threshold의 85%까지 줄이기
MIN_REMAINING_TURNS = 5          # 최소 5턴의 실제 대화 보존
```

### 8.2 동작

```
매 턴 시작 시:
  total_tokens = count_tokens(messages)
  threshold = context_limit * COMPRESS_THRESHOLD_RATIO

  if total_tokens > threshold:
      target = threshold * COMPRESS_TARGET_RATIO
      오래된 턴부터 청크 단위(3~8턴)로 그룹화
      각 청크 → mem0.search()로 해당 기간의 기억 조회 → 요약 텍스트 생성
      원본 메시지를 [요약 user/assistant 쌍]으로 교체
      → immutable chunk (이후 절대 수정 안 함)
```

---

## 9. 캐싱 전략: mem0 + 경량 Curator

### 9.1 문제: mem0만으로는 서사 흐름이 없다

mem0의 add()는 **fact 레벨** 큐레이팅이다:
- "루비아가 플레이어를 좋아함" → UPDATE → "루비아가 플레이어를 매우 좋아함"
- 개별 사실의 추가/수정/삭제는 잘 처리함

하지만 RISU_ENE의 `stable_prefix.md`가 하던 일은 **서사 레벨** 합성이다:
- "플레이어는 심연 주식회사에 입사 후 루비아와 파트너가 되었으며, 첫 던전 탐사에서 C등급 코어를 발견했다"
- 파편화된 fact 목록이 아니라 **흐름이 있는 요약문**

mem0 search()로 fact를 10개 나열하면 LLM이 맥락을 파악하기 어렵다.
서사 요약이 있으면 LLM이 "지금 이야기가 어디까지 왔는지" 즉시 이해한다.

### 9.2 해결: 경량 Curator (stable_prefix.md 대체)

N턴마다 LLM으로 서사 요약을 합성하여 mem0에 특별 메모리로 저장한다.
RISU_ENE의 `CuratorRunner`를 단순화한 버전이다.

```
                    mem0 fact 레벨            경량 Curator 서사 레벨
                    ─────────────            ──────────────────────
매 턴:              add() → fact 추출         (동작 안 함)
                    ADD/UPDATE/DELETE
                    
N턴마다 (기본 10):  (동작 안 함)              기존 서사 요약 조회
                                              + 최근 N턴 대화 읽기
                                              → LLM으로 서사 요약 갱신
                                              → mem0에 metadata.type=
                                                "narrative_summary"로 저장
```

### 9.3 Curator 구현

```python
CURATOR_INTERVAL = 10  # 10턴마다 실행
CURATOR_MODEL = "gpt-4o-mini"  # 요약 전용 — 비용 최소화
NARRATIVE_MAX_CHARS = 1500  # 서사 요약 최대 길이

CURATOR_PROMPT = """
당신은 롤플레이 서사 요약 전문가입니다.

기존 서사 요약과 최근 대화를 읽고, **갱신된 서사 요약**을 작성하세요.

규칙:
1. 시간순으로 주요 사건과 결정을 정리
2. 캐릭터 관계 변화를 반영
3. 현재 상황(위치, 목표, 긴장 관계)을 마지막에 명시
4. {max_chars}자 이내로 작성
5. 일상 대화나 전투 디테일은 생략, 플롯에 영향을 준 것만 포함

기존 서사 요약:
{existing_summary}

최근 대화 ({turn_count}턴):
{recent_conversation}

갱신된 서사 요약:
"""


async def curate_narrative_summary(
    user_id: str,
    agent_id: str,
    app_id: str,
    recent_messages: list[dict],
):
    """N턴마다 비동기 실행. 서사 요약을 갱신하여 mem0에 저장."""

    with tracer.start_as_current_span("curator.narrative") as span:
        # 1. 기존 서사 요약 조회
        existing = await memory.search(
            query="narrative summary",
            user_id=user_id,
            agent_id=agent_id,
            filters={"metadata": {"type": "narrative_summary"}},
            limit=1,
        )
        existing_summary = (
            existing["results"][0]["memory"]
            if existing.get("results")
            else "(아직 서사가 시작되지 않음)"
        )

        # 2. 최근 대화를 텍스트로 변환
        conversation_text = "\n".join(
            f'{m["role"]}: {m["content"][:300]}'
            for m in recent_messages
            if m["role"] in ("user", "assistant")
        )

        # 3. LLM으로 서사 요약 갱신
        prompt = CURATOR_PROMPT.format(
            existing_summary=existing_summary,
            recent_conversation=conversation_text,
            turn_count=len(recent_messages) // 2,
            max_chars=NARRATIVE_MAX_CHARS,
        )
        new_summary = await llm.generate(
            model=CURATOR_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.3,
        )

        # 4. mem0에 저장 — ADD 또는 기존 요약을 UPDATE
        await memory.add(
            messages=[{"role": "system", "content": new_summary}],
            user_id=user_id,
            agent_id=agent_id,
            app_id=app_id,
            metadata={"type": "narrative_summary"},
        )

        span.set_attributes({
            "curator.existing_length": len(existing_summary),
            "curator.new_length": len(new_summary),
            "curator.model": CURATOR_MODEL,
        })
        logger.info(
            "narrative_curated",
            user_id=user_id,
            agent_id=agent_id,
            summary_length=len(new_summary),
        )
```

### 9.4 서사 요약의 생명주기

```
Turn  1: (서사 요약 없음 — fact만 축적)
Turn  2~9: mem0에 fact 축적 (ADD/UPDATE/DELETE)
Turn 10: ★ Curator 실행
          → "(아직 시작되지 않음)" + 최근 10턴 대화
          → LLM 합성: "플레이어는 심연 주식회사에 입사하여..."
          → mem0 저장 (metadata.type = "narrative_summary")

Turn 11~19: fact 축적 계속
Turn 20: ★ Curator 실행
          → 기존 요약 + 최근 10턴 대화
          → LLM 합성: 기존 요약을 확장/수정
          → mem0 UPDATE (기존 요약을 덮어씀)

Turn 30, 40, 50...: 반복
```

### 9.5 컨텍스트 빌드 시 서사 요약 주입

```
Phase 2에서 검색:
  narrative = await memory.search(
      "narrative summary",
      filters={"metadata": {"type": "narrative_summary"}},
      limit=1,
  )

Phase 3 Greedy Assembly에서 최우선 주입:
  ⓪ [서사 요약]           ← curator가 합성한 흐름 있는 요약 (1500자 cap)
  ① [세계관 설정]          ← charx constant 로어북
  ② [캐릭터 정보]          ← triggered 로어북
  ③ [기억]                ← mem0 개별 fact (캐릭터별)
  ④ [세계 이벤트]          ← mem0 공유 fact
```

### 9.6 RISU_ENE curator와의 차이

| | RISU_ENE CuratorRunner | Yang-Ban 경량 Curator |
|---|---|---|
| **저장소** | stable_prefix.md 파일 | mem0 메모리 (metadata.type) |
| **큐레이팅 주체** | Letta 에이전트 (memory block) | 직접 LLM 호출 (gpt-4o-mini) |
| **Memory Block** | narrative_summary, curation_decisions, contradiction_log (3개) | narrative_summary만 (1개) |
| **Letta 의존** | Letta 서버 필요, 장애 시 fallback | 없음 — 직접 LLM 호출 |
| **모순 탐지** | contradiction_log로 추적 | mem0 UPDATE/DELETE가 암묵적으로 처리 |
| **부가 기능** | 로어 자동 생성, NPC 디딥 | 없음 (향후 확장) |
| **비용** | Letta agent context 소비 | gpt-4o-mini 1회 호출 (~$0.001) |

### 9.7 왜 이 방식인가 (설계 근거)

**Q: mem0의 fact를 그냥 나열하면 안 되나?**
A: 50턴 이후 fact가 30개 이상 쌓이면, 개별 fact 나열은:
- 시간순이 아님 (벡터 유사도 순)
- 중복/모순이 있을 수 있음 (mem0가 완벽하지 않음)
- LLM이 "이야기가 어디까지 왔는지" 파악하려면 추론 비용이 큼

서사 요약 1개가 fact 10개보다 LLM에게 효과적인 컨텍스트를 제공한다.

**Q: 왜 Letta를 안 쓰나?**
A: Letta는 에이전트 런타임을 통째로 교체해야 한다. Yang-Ban은 FastAPI 프록시이므로
   mem0 + 직접 LLM 호출이 아키텍처적으로 맞다. Letta의 장점(memory block 연속성)은
   mem0의 UPDATE로 대체 가능하다.

**Q: 10턴 간격이 적절한가?**
A: RISU_ENE에서 검증된 값. 너무 짧으면 비용 낭비, 너무 길면 요약이 stale.
   `CURATOR_INTERVAL` 설정으로 조절 가능.

---

## 10. mem0 add() 내부 파이프라인 (참고)

mem0의 add()는 내부적으로 **LLM 2회** 호출한다. Yang-Ban이 직접 구현할 필요 없이
mem0가 자체적으로 큐레이팅을 수행:

```
add(messages, user_id, agent_id, ...)
│
├─ [1] Fact Extraction (LLM 1회)
│    └─ custom_fact_extraction_prompt 적용
│    └─ {"facts": ["사실1", "사실2", ...]}
│
├─ [2] Embedding
│    └─ 각 fact → embedding 벡터 생성
│
├─ [3] Similarity Search
│    └─ 기존 메모리와 유사도 비교 (중복 확인)
│
├─ [4] Action Determination (LLM 2회)
│    └─ ADD    → 완전히 새로운 정보
│    └─ UPDATE → 기존 메모리 보완/수정
│    └─ DELETE → 새 정보가 기존을 무효화
│    └─ NONE   → 변경 없음
│
├─ [5] Vector Store 반영
│
└─ [6] History 기록 (SQLite 감사 추적)
```

**지연 시간**: add() ~0.7s (p50), search() ~0.15s (p50)

**비교**: RISU_ENE에서는 Flash 추출(Sub-B) + Letta 큐레이터(10턴마다)로 2단계였지만,
mem0는 add() 한 번으로 추출 + 큐레이팅을 통합 수행.

---

## 11. 알려진 mem0 이슈 & 회피책

| 이슈 | 설명 | 회피책 |
|------|------|--------|
| `Memory(config={})` AttributeError (#3496) | dict를 attribute access 시도 | **반드시 `Memory.from_config(config_dict=...)`** 사용 |
| `get_all()` 코루틴 반환 (#2892) | AsyncMemory에서 코루틴 객체 반환 | 최신 버전(0.1.40+)으로 업데이트, 또는 `search()`로 대체 |
| Gemini JSON 파싱 실패 (#3918) | Gemini가 불완전한 JSON 반환 | fact extraction LLM은 **OpenAI gpt-4o-mini** 사용 |
| 배치 연산 없음 (#3761) | 여러 메시지 동시 저장 불가 | `asyncio.gather()`로 병렬화 |
| `custom_update_memory_prompt` 미지원 (#2366) | UPDATE/DELETE 판단 프롬프트 커스텀 불가 | 현재는 fact extraction만 커스터마이즈 |

---

## 12. 향후 확장 로드맵

### Phase A: MVP (현재 설계)
- mem0 단일 vector search
- charx 로어북 (constant + triggered)
- **경량 Curator (N턴마다 서사 요약 합성 → mem0 저장)**
- Greedy Token Allocator
- 3-BP Anthropic Cache
- InstrumentedMemory (OTel + Langfuse)

### Phase B: 관계 그래프
- `enable_graph=True` — 캐릭터 관계 자동 추출
- Neo4j(prod) / Kuzu(dev) 백엔드
- 검색 결과에 `relations[]` 포함 → 컨텍스트에 관계 정보 주입

### Phase C: 멀티소스 검색 (RRF)
- RISU_ENE의 RRF 3-source fusion 포팅
- 소스: recency (최근 기억) + importance (중요 이벤트) + semantic (유사도)
- mem0 metadata 필터링으로 구현 가능

### Phase D: 고급 큐레이팅
- 모순 탐지/해결 로그 (RISU_ENE contradiction_log 패턴)
- mem0 immutable 메모리 (핵심 설정은 삭제 불가)
- `expiration_date`로 임시 상태 자동 만료
- `custom_categories`로 RP 전용 분류 (world_event, relationship, player_action)
- Curator 고도화: 로어 자동 생성, 캐릭터별 서사 분기

---

## 13. 디렉토리 구조 (구현 대상)

```
app/
├── services/
│   ├── context_builder.py    # Phase 1~4 — 이 문서의 핵심
│   ├── curator.py            # 경량 Curator — N턴마다 서사 요약 합성
│   ├── system_stabilizer.py  # canonical system prompt 보호
│   ├── window_recovery.py    # RisuAI 윈도우 이동 감지 & 복구
│   ├── message_compressor.py # LLM-free 턴 압축
│   └── post_turn.py          # Phase 6 비동기 후처리 (curator 트리거 포함)
│
├── memory/
│   ├── base.py               # MemoryBackend 프로토콜
│   ├── mem0_backend.py       # InstrumentedMemory (mem0 + OTel 래퍼)
│   └── factory.py            # MEMORY_BACKEND 환경변수 → 인스턴스 생성
│
├── charx/
│   ├── parser.py             # charx ZIP → card.json 파싱
│   ├── lorebook.py           # 키워드 인덱스 + 매칭 엔진
│   └── schemas.py            # chara_card_v3 Pydantic 모델
│
└── core/
    ├── tracing.py            # OTel + Langfuse 설정
    ├── metrics.py            # Prometheus 메트릭 정의
    └── logging.py            # structlog 설정
```
