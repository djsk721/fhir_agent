# AI Agent 기반 임상 DB → FHIR R4 표준화

이 프로젝트는 병원 PostgreSQL metadata와 FHIR R4 Schema를 검색 가능한
Vector DB로 구축하고, LLM이 안전한 임상 조회 SQL을 생성하도록 지원합니다.
조회된 source row는 LLM이 아니라 결정적 Resource Renderer가 FHIR R4
리소스로 변환합니다.

지원 리소스:

- `Patient`
- `Encounter`
- `Condition`
- `Procedure`
- `Observation`

## 핵심 아키텍처

```text
[사전 구축]
FHIR R4 JSON Schema ─→ fhir_schema_vectors ─┐
                                             ├→ Resource Definition Registry
Source DB Metadata ─→ source_sql_schema_vectors ┘

[환자별 실행]
patient key
  → LLM SQL Query Plan
  → Safe SQL Runtime
  → Source Row Contract
  → Deterministic Resource Renderer
  → Bundle Builder
  → Validation Pipeline
  → Bundle / Validation / Trace / Manifest
```

LLM과 결정적 컴포넌트의 책임은 분리되어 있습니다.

| 단계 | 담당 |
| --- | --- |
| FHIR/DB metadata 수집과 Vector 적재 | 결정적 코드 |
| source relation·join을 선택하는 SQL 생성 | LLM SQL Agent |
| SQL 안전성 검사와 read-only 실행 | Safe SQL Runtime |
| alias·scope·identity 검증 | Source Row Contract |
| source row → FHIR Resource 변환 | 결정적 Renderer |
| ID/reference 정규화와 Bundle 생성 | 결정적 코드 |
| FHIR Schema·참조·원천 충실도 검증 | 결정적 코드 |
| SQL 실패 원인 분석과 재시도 | LLM SQL Agent |

FHIR JSON을 row 단위로 생성하거나 수정하는 LLM 코드는 사용하지 않습니다.
반복 오류는 개별 row가 아니라 SQL Query Plan, Resource Definition 또는
Renderer 정책을 교정해야 합니다.

## Resource Definition

[`resource_definition.py`](./fhir_agent_pipeline/resource_definition.py)는
리소스별 변환 계약을 정의합니다.

각 Definition은 다음 정보를 포함합니다.

- 처리 순서와 리소스 의존성
- 필수 source alias
- 선택 source alias
- source·Bundle 필수 여부와 FHIR 필수 필드
- 리소스 식별 alias와 Encounter scope 정책
- 환경 설정에 연결된 조회 한도
- FHIR 필드 생성 정책
- reference 생성 정책
- `value[x]`, `effective[x]`, `performed[x]` 선택 정책
- status와 pagination 정책
- validation 정책

Definition에는 병원별 물리 테이블명이나 컬럼 매핑을 넣지 않습니다. 실제
relation과 column은 현재 DB 계정으로 조회 가능한 metadata와 Vector 검색
결과를 근거로 SQL Agent가 선택합니다.

SQL Agent는 환자 전체 범위를 다음과 같이 유지합니다.

- Encounter는 ICU stay나 transfer 일부가 아닌 가장 포괄적인 환자-level relation에서 전체 조회
- Condition/Procedure는 조회된 모든 Encounter key를 한 번에 적용하고 event row 전체 보존
- Observation은 Encounter 유무와 관계없이 환자 전체 event row 조회
- dictionary는 문서화된 복합키 전체를 사용하는 LEFT JOIN만 허용

## Source Row Contract

SQL Agent는 리소스별 공통 alias를 반환해야 합니다.

| Resource | 주요 필수 alias |
| --- | --- |
| Patient | `_patient_key` |
| Encounter | `_patient_key`, `_encounter_key`, `_encounter_type` |
| Condition | `_patient_key`, `_encounter_key`, `_resource_key`, `_code` |
| Procedure | `_patient_key`, `_encounter_key`, `_resource_key`, `_code` |
| Observation | `_patient_key`, `_resource_key`, `_code`, `_event_datetime` |

Observation의 `_encounter_key`는 선택 alias입니다. Observation SQL은 선택된
Encounter 목록으로 범위를 축소하지 않고 환자 전체 event row를 조회합니다.
원본 event에 Encounter 키가 없거나 생성 Bundle에 대응 Encounter가 없으면
값을 만들지 않고 `Observation.encounter`를 생략합니다.

Renderer 실행 전 다음을 검사합니다.

- Definition에 선언된 필수·선택 alias가 SQL 결과에 존재하는지
- 필수 alias 값이 비어 있지 않은지
- `_patient_key`가 요청 환자와 일치하는지
- 임상 리소스의 `_resource_key`가 고유한지
- Condition/Procedure가 선택된 Encounter 범위에 속하는지

## 결정적 Resource Renderer

[`resource_renderer.py`](./fhir_agent_pipeline/resource_renderer.py)의
`DeterministicResourceRenderer`는 각 source row를 정확히 하나의 FHIR
Resource로 변환합니다.

주요 정책:

- source key 기반의 안정적인 FHIR ID
- Patient/Encounter reference 자동 생성
- Observation은 생성된 Encounter와 원본 키가 일치할 때만 Encounter reference 생성
- 원천에 없는 code system, display, unit system을 생성하지 않음
- 숫자값이 있으면 `valueQuantity`, 없고 text가 있으면 `valueString`
- timezone이 없는 timestamp에 `FHIR_DEFAULT_TIMEZONE` 적용
- 빈 객체와 빈 배열 제거
- Observation reference range와 interpretation을 source alias에서 생성
- 렌더링 직후 local FHIR JSON Schema와 필수 필드 검사

`build_bundle_pages()`는 기본 임상 리소스와 대량 Observation을 설정된 page
크기로 분리할 수 있습니다. 현재 기본 export 명령은 환자별 단일 collection
Bundle을 저장합니다.

## SQL 안전성 및 Self-Correction

LLM SQL에는 다음 정책이 적용됩니다.

- `SELECT` 또는 `WITH` 한 문장만 허용
- 현재 DB 계정으로 발견된 `schema.table`만 허용
- DDL, DML, row lock, SQL comment 차단
- 서버 파일, 외부 연결, 지연 함수 차단
- read-only transaction과 statement timeout
- 최종 row limit 강제
- required alias, patient scope, Condition/Procedure Encounter scope, key 고유성 검증

실패하면 이전 SQL, 오류 유형과 관련 relation metadata를 LLM에 다시 제공하여
SQL Query Plan을 교정합니다. 선택 alias가 실제 source column으로 입증되지
않으면 임의 값을 만들지 않고 `NULL`로 낮춥니다.

동일한 `_resource_key` 실패 SQL이 반복되면 특정 source 컬럼명을 가정하지
않고 DB catalog의 PK metadata를 우선 사용합니다. PK가 없는 event relation은
선택된 source row 전체의 canonical JSON hash를 fallback identity로 사용하며,
dictionary join으로 같은 event가 증식한 경우에는 동일 hash가 충돌하므로
오류를 숨기지 않습니다.

## 설치와 설정

```bash
python -m pip install -r requirements.txt
cp .env.example .env
```

필수 설정 예:

```dotenv
SOURCE_PROFILE_PATH=./profiles/fhir_ndjson_cohort/profile.json
SOURCE_DB_URL=postgresql+psycopg2://user:password@db-host:5432/clinical
SOURCE_DATA_PATH=./data/fhir-cohort

OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=gemma4:e4b
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
LLM_SQL_MAX_ATTEMPTS=5

FHIR_SCHEMA_PATH=./db/fhir.schema.json
FHIR_DEFAULT_TIMEZONE=Z

SQL_MAX_ROWS=10000
SOURCE_MAX_ENCOUNTERS=100
SOURCE_MAX_OBSERVATIONS_PER_ENCOUNTER=2000
SOURCE_MAX_CONDITIONS_PER_ENCOUNTER=1000
SOURCE_MAX_PROCEDURES_PER_ENCOUNTER=1000

VECTOR_SCHEMA=public
FHIR_VECTOR_TABLE=fhir_schema_vectors
SQL_VECTOR_TABLE=source_sql_schema_vectors
VECTORS_ENABLED=true
```

추출 범위에 영향을 주는 `SQL_MAX_ROWS`와 `SOURCE_MAX_*` 값은 숨은 코드
기본값을 사용하지 않으며 반드시 환경 변수로 명시해야 합니다. 대상 데이터
규모에 맞게 설정하고, 결과 row 수가 설정값에 도달하면 trace와 원천 count를
확인해 잘림 여부를 검토합니다.

DB 접근 범위는 애플리케이션 table allowlist가 아니라 FHIR 변환 전용 read-only
DB 계정의 권한으로 통제합니다. `.env`와 export 결과는 Git에 포함하지
않습니다.

## 실행

연결과 설정 확인:

```bash
python main.py doctor
```

FHIR Schema와 Source DB Schema Vector 구축:

```bash
python main.py build-vectors
python main.py inspect-vectors
```

자연어 SQL 확인:

```bash
python main.py ask "환자 key 123의 최근 방문을 조회해줘"
```

환자 한 명 변환:

```bash
python main.py convert --patient-key 123
```

환자별 저장과 독립 검증:

```bash
python main.py export-validate \
  --patient-key 123 \
  --output-dir ./fhir_export_run
```

cohort 일부 또는 전체:

```bash
python main.py export-validate --limit 10
python main.py export-validate --all
python main.py export-validate --all --resume
```

저장된 결과 재검증:

```bash
python main.py validate-export ./fhir_export_run
python main.py validate ./fhir_export_run/patient-123.bundle.json
```

## 출력

| 파일 | 내용 |
| --- | --- |
| `patient-<key>.bundle.json` | 환자별 FHIR collection Bundle |
| `patient-<key>.validation.json` | Schema·구조·참조·원천 충실도 검증 |
| `patient-<key>.trace.json` | SQL, RAG, retry와 Renderer 지표 |
| `manifest.json` | cohort 집계와 성능 지표 |

Trace의 실행 mode는 `llm-sql-and-deterministic-fhir`입니다. 전체
`llm_calls_used`는 SQL 생성과 SQL self-correction 호출만 의미합니다.
`rendering_metrics`의 `llm_calls_used`는 항상 0입니다.

## 검증 및 성능 지표

검증 계층:

1. FHIR R4 local JSON Schema
2. Bundle 구조와 필수 리소스
3. ID 중복과 reference resolution
4. source row 대비 field accuracy
5. resource count alignment
6. code, value, unit, time accuracy
7. 선택적 HL7 FHIR Validator CLI

`FHIR_VALIDATOR_JAR`가 설정되면 공식 Validator CLI를 실행합니다. 원격
terminology 서비스 연결이 허용되지 않거나 불안정한 실행 환경에서는
`FHIR_VALIDATOR_TERMINOLOGY_SERVER=n/a`를 설정해 로컬 구조 검증을
결정적으로 수행할 수 있습니다. 특정 terminology 서버를 사용할 때는
동일한 환경 변수에 해당 서버 주소를 설정합니다.

대표 성능 지표:

- FHIR Validation Pass Rate
- Source Field Accuracy
- Resource Count Alignment
- Condition/Procedure/Observation precision, recall, F1
- Observation value/unit/time accuracy
- Reference Resolution Rate
- Coding Accuracy
- SQL first-pass success와 repair attempts
- RAG ablation improvement

## 다른 병원 DB 적용

1. FHIR 변환용 read-only DB 계정을 생성합니다.
2. `.env`의 `SOURCE_DB_URL`과 source profile을 변경합니다.
3. `doctor`로 현재 계정에서 보이는 relation을 확인합니다.
4. `build-vectors`로 DB metadata Vector를 다시 구축합니다.
5. 소수 환자로 실행하고 trace와 validation을 확인합니다.
6. 문제가 반복되면 물리 매핑을 코드에 추가하지 말고 DB comment, PK/FK,
   SQL 질문 또는 Resource Definition 정책을 교정합니다.

## 테스트 노트북

[`test.ipynb`](./test.ipynb)는 다음 순서로 현재 TASK를 확인합니다.

1. 설정과 Resource Definition
2. 결정적 Renderer 단위 실행
3. cohort와 DB catalog
4. FHIR/DB Vector DB
5. LLM SQL과 Safe SQL Runtime
6. 단일 환자 전체 파이프라인
7. Bundle page와 validation
8. 선택적 cohort batch

외부 DB, Ollama, Vector rebuild와 batch 실행 플래그는 기본적으로 꺼져
있습니다.

## 회귀 테스트

```bash
python -m pytest -q
python -m compileall -q fhir_agent_pipeline main.py
```
