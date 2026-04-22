# MIMIC-IV Multi-Agent FHIR Pipeline

이 프로젝트는 **MIMIC 스타일 임상 데이터**를 입력받아, **리소스별 LLM SQL planning + RAG grounding + 다중 에이전트 self-correction**을 통해 **FHIR 리소스와 Bundle**을 생성하는 시스템입니다.

현재 다음 실행 방식을 지원합니다.

- **CLI 실행**: `python main.py`
- **FastAPI 백엔드**: `uvicorn main:app --host 0.0.0.0 --port 8000`
- **Next.js 프론트엔드**: `frontend/`에서 `npm run dev`

---

## 1. 핵심 기능

### 1.1 Resource별 LLM SQL → Mapping → FHIR 변환
- 입력: `patient`, `admission`, `lab_event` 중심의 seed JSON
- 대상 리소스는 `.env`의 `FHIR_TARGET_RESOURCES` 또는 API의 `resource_types`로 결정됩니다.
- 각 리소스별로:
  1. SQL Planner Agent가 안전한 SELECT 계획을 생성하고
  2. SafeSqlRuntime이 검증/실행하며
  3. Mapping Agent가 raw payload + SQL evidence + retrieval context를 임상 의미 중심 JSON으로 정규화하고
  4. FHIR Generator Agent가 리소스를 생성한 뒤
  5. Verifier Agent + 로컬 validator + Manager Agent의 자기 수정 루프를 거쳐
  6. 최종 FHIR Bundle을 반환합니다.

### 1.2 리소스별 SQL Planning
- `SqlPlannerAgent`가 리소스 타입에 맞는 **JSON SQL 계획**을 생성합니다.
- 기본 허용 테이블 예시는 다음과 같습니다.
  - `Patient` → `patients`
  - `Encounter` → `admissions + patients`
  - `Observation` → `labevents + admissions + patients + d_labitems`
- 실제 SQL 실행은 LLM이 아니라 `SafeSqlRuntime`이 수행합니다.

### 1.3 LLM 기반 Mapping
- `MappingAgent`는 다음 입력을 함께 사용합니다.
  - raw seed payload
  - resource별 `sql_evidence`
  - retrieval context
- 반환 구조는 다음 키를 중심으로 구성됩니다.
  - `patient_demographics`
  - `encounter_context`
  - `observation_context`
  - `terminology_mapping`
  - `notes`

### 1.4 RAG 기반 Grounding
- SQL planning, mapping, FHIR generation, verifier 단계 모두 retrieval context를 활용합니다.
- 우선 사용 코퍼스:
  - `fhir_schema_vectordb/`
  - `mimic_docs_vectordb/` (존재 시)
- vector retrieval이 불가능하면 `db/fhir.schema.json` 기반 fallback으로 동작합니다.

### 1.5 FHIR 생성 + 검증 + 자기 수정 루프
- `FhirGeneratorAgent`가 리소스 JSON을 생성합니다.
- 생성 후 일부 리소스에는 **post-generation normalization**이 적용됩니다.
  - `Patient` → 성별/생년 보정
  - `Encounter` → `class` 보정
- 이후 다음 두 검증이 함께 실행됩니다.
  1. `VerifierAgent`의 LLM 검증
  2. `validate_fhir_json()` / `validate_bundle()`의 로컬 구조 검증
- 실패 시 `ManagerAgent`가 correction instruction을 생성하고 재시도합니다.
- 재시도 피드백에는 **LLM verifier 오류와 로컬 schema 오류가 모두 포함**됩니다.

### 1.6 웹 UI 제공
- Next.js App Router 기반 프론트엔드 제공
- 화면에서 다음 기능을 사용할 수 있습니다.
  - JSON 입력
  - Mapping 실행
  - Analyze 실행
  - FHIR 결과 확인
  - Debug / trace 확인

---

## 2. 실제 동작하는 API

루트 엔드포인트 `/`는 서비스 상태와 사용 가능한 라우트 목록을 반환합니다.

```json
{
  "service": "FHIR Agent Service",
  "status": "ok",
  "frontend_url": "http://localhost:3000",
  "available_routes": ["/analyze", "/mapping", "/schema"]
}
```

### `POST /mapping`
Observation 중심의 SQL planning + mapping만 수행합니다.

#### 요청 모델
- `itemid: int | str | null`
- `payload: MimicPayload | null`
- `notes: str | null`

`MappingRequest`는 **`itemid` 또는 `payload` 중 하나는 반드시 제공되어야 합니다.**

#### 입력 예시
```json
{
  "itemid": 50868,
  "payload": {
    "patient": {"subject_id": 10000032},
    "admission": {"hadm_id": 22595853},
    "lab_event": {"itemid": 50868, "label": "ANION GAP"}
  }
}
```

#### 반환
- `itemid`
- `mapping_payload`
- `mapping_explanation`
- `debug_context`
  - `agent_sequence`
  - `rag_context` (SQL trace 포함)
  - `schema_context`
  - `source_payload_summary`
  - `warnings`

#### 동작 특징
- `/mapping`은 현재 `Observation` 리소스 기준으로 retrieval 및 SQL evidence를 구성합니다.
- payload 없이 `itemid`만 전달한 경우 minimal payload를 구성해 mapping을 시도하며, 경고 메시지를 반환합니다.

---

### `POST /analyze`
전체 파이프라인을 수행합니다.

#### 요청 모델
- `payload: MimicPayload`
- `resource_types: list[str] | null`

`resource_types`를 전달하면 `.env`의 기본 리소스 목록 대신 해당 목록으로 파이프라인을 구성합니다.

#### 입력 예시
```json
{
  "payload": {
    "patient": {"subject_id": 10000032},
    "admission": {"hadm_id": 22595853},
    "lab_event": {"itemid": 50868}
  },
  "resource_types": ["Patient", "Encounter", "Observation"]
}
```

#### 반환
- `mapping_payload`
- `generated_resources`
- `fhir_bundle`
- `verification_errors`
- `attempts_used`
- `debug_context`
  - `agent_sequence`
  - `rag_context` (retrieval trace + SQL trace)
  - `schema_context`
  - `source_payload_summary`
  - `warnings`

#### 동작 특징
- 리소스 생성 순서는 `Patient -> Encounter -> Observation`입니다.
- 리소스 생성 실패 시 partial bundle과 함께 현재까지의 `verification_errors`를 반환할 수 있습니다.
- `payload.lab_event`가 없으면 Observation 근거가 제한되었다는 warning이 추가될 수 있습니다.

---

### `GET /schema`
로컬 FHIR schema 일부를 반환합니다.

#### 쿼리 파라미터
- `resource_types: list[str] | null`

#### 기본 지원 리소스
- `Patient`
- `Encounter`
- `Observation`
- `Bundle`

#### 반환
- `source`
- `supported_resource_types`
- `definitions`

---

## 3. 구현 구조

### 3.1 전체 아키텍처

```text
Frontend (Next.js)
  -> FastAPI (/analyze, /mapping, /schema)
    -> app/services/*
      -> fhir_agent_pipeline/*
        -> RuntimeRetriever
        -> SqlPlannerAgent
        -> SafeSqlRuntime
        -> MappingAgent
        -> FhirGeneratorAgent
        -> VerifierAgent
        -> ManagerAgent
        -> Local FHIR validators
```

주요 디렉터리:

- `frontend/`
  - Next.js App Router UI
- `app/api/`
  - FastAPI route 정의
- `app/services/`
  - API 요청을 실제 pipeline/runtime 호출로 연결하는 서비스 계층
- `app/models/`
  - request/response 모델
- `fhir_agent_pipeline/`
  - 실제 LLM + SQL + retrieval + FHIR 변환 로직

---

### 3.2 `/analyze` 파이프라인 처리 흐름

1. 입력 payload와 대상 resource list를 결정합니다.
2. `RuntimeRetriever`가 mapping용 / resource별 generation용 retrieval context를 구성합니다.
3. `SqlPlannerAgent`가 리소스별 SQL 계획을 생성합니다.
4. `SafeSqlRuntime`이 SQL을 검증/실행하고 `sql_evidence`를 구성합니다.
5. `MappingAgent`가 raw payload + `sql_evidence` + retrieval context로 mapping JSON을 생성합니다.
6. 각 리소스를 순서대로 생성합니다.
   - `Patient`
   - `Encounter`
   - `Observation`
7. 생성된 리소스에 normalization을 적용합니다.
   - `normalize_patient_resource()`
   - `normalize_encounter_resource()`
8. `VerifierAgent`와 로컬 validator가 함께 검증합니다.
9. 실패 시 `ManagerAgent`가 correction instruction을 만들고 재생성합니다.
10. 모든 리소스가 성공하면 `build_bundle()`로 최종 Bundle을 생성합니다.
11. `validate_bundle()`로 Bundle 전체를 검증합니다.
12. retrieval trace와 SQL trace는 `debug_context.rag_context`로 노출됩니다.

---

### 3.3 LLM 구현 방식

활성 런타임은 **Ollama + LangChain** 기반입니다.

- 채팅 모델: `langchain_ollama.ChatOllama`
- 공통 베이스: `BaseAgent`
- 주요 에이전트:
  - `SqlPlannerAgent`
  - `MappingAgent`
  - `FhirGeneratorAgent`
  - `VerifierAgent`
  - `ManagerAgent`

각 에이전트는 `fhir_agent_pipeline/prompts.py`에 정의된 시스템/유저 프롬프트를 사용하며, 응답은 JSON-only를 전제로 파싱됩니다.

현재 JSON 응답 파서는:
- fenced code block 대응
- 응답 내 첫 complete JSON block 추출
- malformed JSON에 대해서는 명시적 실패
를 수행하도록 보강되어 있습니다.

---

### 3.4 SQL 생성 및 실행 방식

SQL 생성은 `SqlPlannerAgent`, 실행은 `SafeSqlRuntime`이 담당합니다.

#### 동작 방식
- resource type별 허용 테이블을 결정합니다.
- SQLAlchemy inspector로 사용 가능한 테이블/컬럼 metadata를 조회합니다.
- LLM이 단일 SELECT SQL + params JSON을 생성합니다.
- `SafeSqlRuntime`이 아래 규칙으로 검증합니다.
  - SELECT only
  - comment / multi-statement 금지
  - 허용 테이블만 사용
  - LIMIT 강제
  - bind parameter 사용

#### allowlist 처리
- `.env`의 `FHIR_ALLOWED_TABLES`가 있으면 해당 값 우선
- 없으면 resource별 기본 allowlist 사용

#### 실패 전략
- `SQL_FAIL_OPEN=true`면 SQL 계획/실행 실패 시 빈 evidence와 trace 오류를 남기고 계속 진행
- `SQL_FAIL_OPEN=false`면 예외를 전파할 수 있습니다.

---

### 3.5 RAG 구현 방식

RAG는 `RuntimeRetriever`가 담당합니다.

#### 동작 방식
- mapping query와 resource별 generation query를 따로 구성합니다.
- Chroma vector store에서 유사 문서를 검색합니다.
- mapping용 context와 resource별 context를 각각 생성합니다.
- retrieval 실패 시 local schema JSON fallback을 사용합니다.

#### 현재 retrieval 소스
- `FHIR_SCHEMA_DB_DIR`
- `MIMIC_KNOWLEDGE_DB_DIR`

#### trace 정보
`debug_context.rag_context`에는 다음 정보가 포함될 수 있습니다.
- corpus
- purpose (`mapping`, `generation:Patient` 등)
- status (`vector`, `fallback_json`, `error`, `disabled` 등)
- query
- hit_count
- detail

---

### 3.6 Normalization 동작

현재 구현에는 생성 직후 적용되는 normalization 단계가 있습니다.

#### Patient normalization
- 성별 문자열을 FHIR administrative gender로 보정
  - 예: `m` -> `male`, `f` -> `female`
- `birthDate`가 없거나 잘못된 경우:
  - 직접 제공된 생년 문자열 사용 시도
  - 없으면 `anchor_year - anchor_age` 기반으로 `YYYY-01-01` 계산

#### Encounter normalization
- 이미 유효한 `class`가 있으면 유지
- 없으면 `admission`, `hospitalization`, `type`, `serviceType`, `sql_evidence["Encounter"]` 등을 기반으로 `class`를 유도
- 현재 다음 ActCode 계열 값을 생성할 수 있습니다.
  - `EMER`
  - `OBSENC`
  - `AMB`
  - `IMP`
- 근거가 부족하면 무리하게 채우지 않고 validator가 실패하도록 둡니다.

> 참고: ED timestamp와 입원 유형이 동시에 존재하는 mixed-evidence case는 향후 더 정교한 precedence tuning 대상입니다.

---

### 3.7 FHIR 검증 및 자기 수정

검증은 2단계입니다.

1. **LLM verifier**
   - `VerifierAgent`가 리소스를 검토

2. **로컬 validator**
   - `fhir_agent_pipeline/fhir_models.py`
   - `validate_fhir_json()`
   - `validate_bundle()`

검증 실패 시:
- LLM verifier 오류와 로컬 validator 오류를 합쳐서
- `ManagerAgent.review_feedback()`에 전달하고
- `correction_instructions`를 생성해 재시도합니다.

즉 현재 self-correction loop는 **LLM 피드백만이 아니라 로컬 schema 오류도 재생성 피드백으로 사용**합니다.

---

## 4. 주요 파일 구조

```text
project/
├── app/
│   ├── api/
│   ├── core/
│   ├── models/
│   ├── services/
│   └── main.py
├── frontend/
├── fhir_agent_pipeline/
│   ├── agents.py
│   ├── build_vector_stores.py
│   ├── config.py
│   ├── db.py
│   ├── fhir_models.py
│   ├── pipeline.py
│   ├── prompts.py
│   ├── retrieval.py
│   └── sql_runtime.py
├── tests/
├── db/
│   └── fhir.schema.json
├── fhir_schema_vectordb/
├── mimic_docs_vectordb/
├── main.py
└── requirements.txt
```

---

## 5. 환경 변수

주요 환경 변수:

- `MIMIC_DB_URL`
- `MIMIC_SCHEMA`
- `MIMIC_PATIENTS_TABLE`
- `MIMIC_ADMISSIONS_TABLE`
- `MIMIC_LABEVENTS_TABLE`
- `MIMIC_D_LABITEMS_TABLE`
- `PATIENT_SUBJECT_ID`
- `OLLAMA_BASE_URL`
- `OLLAMA_MODEL`
- `OLLAMA_EMBED_MODEL`
- `FHIR_TARGET_RESOURCES`
- `FHIR_ALLOWED_TABLES`
- `MAX_SELF_CORRECTION_ATTEMPTS`
- `RAG_ENABLED`
- `RAG_TOP_K`
- `RAG_FAIL_OPEN`
- `RAG_EMBEDDING_PROVIDER`
- `RAG_EMBEDDING_MODEL`
- `FHIR_SCHEMA_JSON_PATH`
- `FHIR_SCHEMA_DB_DIR`
- `MIMIC_KNOWLEDGE_DB_DIR`
- `SQL_ENABLED`
- `SQL_FAIL_OPEN`
- `SQL_ROW_LIMIT`
- `SQL_PLANNER_RETRIES`

---

## 6. 설치

### Python
```bash
pip install -r requirements.txt
```

### Frontend
```bash
cd frontend
npm install
```

---

## 7. 실행 방법

### 7.1 CLI
```bash
python main.py
```

CLI는 현재 seed payload를 기반으로 전체 pipeline을 수행하고, verification error가 없으면 최종 FHIR Bundle JSON을 출력합니다.

### 7.2 Backend
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 7.3 Frontend
```bash
cd frontend
npm install
npm run dev
```

브라우저 접속:

```text
http://localhost:3000
```

프론트엔드는 기본적으로 `http://localhost:8000` 백엔드를 호출하며, 필요하면 `NEXT_PUBLIC_API_BASE_URL`로 변경할 수 있습니다.

---

## 8. 현재 검증 상태

현재 코드 기준 확인된 사항:

- FastAPI 앱이 `/analyze`, `/mapping`, `/schema`를 노출함
- `/mapping`, `/analyze`가 `debug_context`에 retrieval trace 및 SQL trace를 포함해 반환함
- JSON 응답 파서 관련 회귀 테스트 존재
- schema validation feedback이 self-correction loop에 반영되는 테스트 존재
- `Encounter.class` normalization 테스트 존재
- `python -m unittest discover -s tests -p "test_*.py"` 기준 테스트가 통과하도록 유지되고 있음

현재 `tests/`에는 예를 들어 다음 검증이 포함됩니다.
- `test_agents_json_parsing.py`
- `test_pipeline_self_correction.py`
- `test_encounter_normalization.py`

---

## 9. 현재 한계와 개선 포인트

현재 구현은 실제 live SQL + RAG 경로를 갖고 있지만, 다음은 여전히 개선 여지가 있습니다.

- LOINC / SNOMED 전용 retrieval corpus 강화
- 더 넓은 resource/table allowlist 확장
- retrieval quality / score threshold 튜닝
- self-correction 단계의 더 정교한 재계획 정책
- Encounter mixed-evidence precedence 개선
  - 예: ED timestamp가 있으면서 동시에 inpatient/observation evidence도 존재하는 경우
- schema 외 의료 문서 코퍼스 품질 향상

---

## 10. 요약

이 프로젝트는 단순한 LLM 변환기가 아니라,

- **MIMIC seed 입력을 받고**
- **resource별 SQL을 LLM이 계획하고 SafeSqlRuntime이 실행한 뒤**
- **RAG로 schema / 의료 문서 grounding context를 검색하고**
- **LLM agent들이 mapping / generation / verification / correction을 수행하고**
- **post-generation normalization과 local validation까지 거쳐**
- **FHIR Bundle을 반환하는 시스템**입니다.

즉 현재 구현은:

- 고정 SQL 기반 prototype이 아니라
- 실제 API / CLI / Frontend에 연결된
- **resource-aware LLM SQL + RAG + self-correction + normalization runtime** 입니다.
