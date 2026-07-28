# DB → LLM SQL → FHIR 변환

이 프로젝트는 PostgreSQL의 스키마를 Vector DB로 만들고, LLM이 그 정보를
검색해 SQL을 생성한 뒤 조회 결과를 FHIR R4 리소스로 변환하고 검증합니다.

핵심 흐름은 다음과 같습니다.

```text
PostgreSQL metadata
  → DB Schema Vector DB
  → LLM Encounter SQL + 동적 relation lineage
  → lineage를 공유한 리소스별 LLM SQL 생성·실행
  → 실행/결과/관계 Self-Correction
  → generic source alias
  → LLM FHIR 생성 + 결정적 정규화
  → Bundle
  → JSON Schema + 구조/참조/원천 충실도 + HL7 FHIR Validator
```

지원 리소스는 `Patient`, `Encounter`, `Condition`, `Procedure`,
`Observation`입니다.

## 중요한 설계 원칙

- MIMIC 또는 병원별 테이블명·컬럼명 목록을 코드에 두지 않습니다.
- 테이블 매핑 파일과 정책 매핑 파일을 사용하지 않습니다.
- PostgreSQL에 연결할 때 `public`, `information_schema`, `pg_*`만 제외하고
  나머지 사용자 스키마의 모든 테이블을 자동 탐색합니다.
- SQL 허용 목록은 실행 시 발견한 `schema.table` 목록 그 자체입니다.
- SQL LLM은 `source_sql_schema_vectors`에 저장된 테이블·컬럼·PK·FK
  메타데이터를 검색해 relation과 join을 선택합니다.
- Encounter SQL에서 실제 사용한 relation과 관련 metadata를 실행 시점에
  추출해 후속 임상 리소스 SQL의 RAG 질문에 전달합니다. 이는 저장된 병원별
  매핑이 아니라 환자별 SQL lineage입니다.
- `_stay_key`, 특정 입원 컬럼 또는 상·하위 진료 구조를 공통 계약에 강제하지
  않습니다. 후속 relation이 Encounter 키를 직접 저장한다고 가정하지 않고,
  검색된 PK/FK와 metadata로 동적 join 경로를 찾습니다.
- 코드체계 URI, display, UCUM 단위처럼 DB에 없는 값은 추측하지 않고
  `NULL`로 둡니다.
- Source profile은 DB 매핑이 아닙니다. 공통 질문 계약, Bundle namespace,
  선택적인 Patient NDJSON cohort 읽기 방식만 정의합니다.

MIMIC demo를 사용할 때만
[`profiles/mimic_iv/profile.json`](./profiles/mimic_iv/profile.json)을 선택합니다.
다른 DB는 이 파일을 복사해 테이블 매핑을 작성할 필요가 없습니다. 직접 환자
key로 실행할 경우 cohort provider가 `none`인 profile도 사용할 수 있습니다.

## 특정 병원 DB 정보의 범위

실행 Python 코드에는 MIMIC 스키마·테이블·컬럼명이 없습니다. 예를 들어
`mimiciv_hosp`, `admissions`, `labevents`, `subject_id`, `hadm_id` 같은 이름을
SQL 생성 규칙이나 FHIR 변환 로직에 고정하지 않습니다.

특정 데이터 소스 정보가 존재하는 위치와 역할은 다음과 같습니다.

| 위치 | 포함 정보 | 변환 결과에 미치는 영향 |
| --- | --- | --- |
| `.env` | 현재 DB 접속 URL, source profile, cohort 데이터 경로 | 연결할 DB와 대상 cohort 선택 |
| `profiles/mimic_iv/profile.json` | demo Patient 파일 패턴, identifier URI, Bundle namespace | 환자 key 추출과 Bundle 식별자 namespace |
| `source_sql_schema_vectors` | 현재 DB에서 동적으로 읽은 실제 schema/table/column | LLM이 SQL relation과 join을 검색 |
| 환자별 `.trace.json` | 해당 실행에서 LLM이 선택한 실제 SQL과 relation | 실행 근거와 self-correction 감사 기록 |
| `tests/`, `test.md`, README | fixture와 연구 설명 | 운영 실행에는 영향 없음 |

따라서 Vector DB와 trace에는 현재 연결된 DB의 실제 테이블명이 나타나는 것이
정상입니다. 이는 코드 하드코딩이나 매핑 파일이 아니라 실행 시 DB metadata에서
수집된 결과입니다. 타 병원으로 전환할 때는 `.env`의 `SOURCE_DB_URL`,
`SOURCE_PROFILE_PATH`, `SOURCE_DATA_PATH`를 교체하고 Vector DB를 다시
구축합니다.

`.env`에는 접속 비밀번호가 포함될 수 있으므로 Git에 커밋하지 않습니다.
현재 `.gitignore`는 `.env`와 모든 `fhir_export*` 결과 디렉터리를 제외합니다.
운영 환경에서는 환경변수 또는 secret manager 사용을 권장합니다.

## 설치

```bash
python -m pip install -r requirements.txt
cp .env.example .env
```

PostgreSQL 서버에는 읽기 권한이 있어야 하며, Vector 테이블을 둘 스키마에는
생성·쓰기 권한이 필요합니다. pgvector 확장과 Ollama도 실행되어야 합니다.

## `.env`

가장 먼저 아래 값을 수정합니다.

```dotenv
SOURCE_PROFILE_PATH=./profiles/fhir_ndjson_cohort/profile.json
SOURCE_DB_URL=postgresql+psycopg2://user:password@db-host:5432/clinical
SOURCE_DATA_PATH=./data/fhir-cohort

OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=gemma4:e4b
OLLAMA_EMBEDDING_MODEL=nomic-embed-text

FHIR_SCHEMA_PATH=./db/fhir.schema.json
FHIR_VALIDATOR_JAR=/path/to/validator_cli.jar

VECTOR_SCHEMA=public
FHIR_VECTOR_TABLE=fhir_schema_vectors
SQL_VECTOR_TABLE=source_sql_schema_vectors
VECTOR_SEARCH_K=30
VECTORS_ENABLED=true
```

전체 설정과 기본값은 [`.env.example`](./.env.example)을 참고합니다.
DB 비밀번호가 없다면 URL에서 `:password`를 빼고
`postgresql+psycopg2://user@db-host:5432/clinical` 형식으로 사용할 수
있습니다. 비밀번호가 필요한 별도 DB 서버라면 해당 서버 계정을 사용합니다.

현재 로컬 `.env`는 다음 운영 상태에 맞춰져 있습니다.

- source profile과 data path: MIMIC FHIR demo cohort 선택
- DB URL: 현재 별도 PostgreSQL 서버 연결값 유지
- `SOURCE_PATIENT_ID`: 비움. 단일 환자는 CLI의 `--patient-key` 사용
- `FHIR_EXPORT_DIR`: `./fhir_export_demo_generic_10`
- `FHIR_OUTPUT_RESOURCES`: 현재
  `Patient,Encounter,Condition,Procedure,Observation` 전체 5종
- Ollama model: `gemma4:e4b`
- `SQL_MAX_ROWS`: 10,000건
- Encounter당 Observation/Condition/Procedure 요청 상한:
  2,000 / 1,000 / 1,000건
- `LLM_FHIR_BATCH_SIZE`: 2건. 대량 실행에서도 gemma 응답 안정성을 우선
- SQL/FHIR Vector 테이블: `source_sql_schema_vectors`,
  `fhir_schema_vectors`

CLI에 `--output-dir`를 지정하면 `FHIR_EXPORT_DIR`보다 해당 경로를 우선합니다.

이 값 중 DB URL, source profile, data path는 타 병원 전환 시 교체해야 하는
환경별 설정이며 테이블·컬럼 매핑은 아닙니다.

`SQL_EXCLUDED_SCHEMAS`, `SQL_ALLOWED_SCHEMAS`, `SQL_ALLOWED_TABLES`,
`SOURCE_SCHEMA`, `SOURCE_MAPPING_CONFIG`는 더 이상 사용하지 않습니다.
변환 대상은 DB metadata에서 자동 결정됩니다.

## 1. 연결 확인

```bash
python main.py doctor
```

출력에서 다음을 확인합니다.

- DB 연결 상태
- 자동 발견한 사용자 스키마와 테이블 수
- FHIR JSON Schema 경로
- cohort Patient 파일 경로
- Ollama 주소와 PostgreSQL 드라이버

## 2. Vector DB 구축

```bash
python main.py build-vectors
python main.py inspect-vectors
```

두 Vector 테이블의 역할은 다음과 같습니다.

| 테이블 | 내용 |
| --- | --- |
| `fhir_schema_vectors` | `db/fhir.schema.json`의 FHIR R4 정의와 속성 |
| `source_sql_schema_vectors` | 자동 발견한 모든 사용자 테이블의 schema, table, column, PK, FK |

같은 문서는 content hash로 재사용하고, 스키마에서 사라진 문서는 정리합니다.
병원별 매핑 문서는 적재하지 않습니다.

2026-07-26 현재 연결된 DB에서 확인한 결과:

- 발견 스키마: 3개
- 발견 테이블: 59개
- SQL Schema Vector: 59건
- FHIR Schema Vector: 11,499건
- embedding dimension: 768
- 두 테이블 모두 HNSW index 사용

이 숫자는 현재 연결 DB의 결과이며 다른 DB에서는 자동으로 달라집니다.

## 3. 실행

자연어 SQL만 확인:

```bash
python main.py ask "환자 key 123의 최근 방문을 조회해줘"
```

환자 1명 변환:

```bash
python main.py convert --patient-key 123
```

환자 1명 생성부터 저장·검증까지:

```bash
python main.py export-validate \
  --patient-key 123 \
  --output-dir ./fhir_export_run
```

여러 환자는 `--patient-key`를 반복합니다.

```bash
python main.py export-validate \
  --patient-key 123 \
  --patient-key 456
```

Source profile의 Patient NDJSON cohort 앞 10명:

```bash
python main.py export-validate \
  --limit 10 \
  --output-dir ./fhir_export_demo_10
```

Source profile의 Patient NDJSON cohort 전체:

```bash
python main.py export-validate \
  --all \
  --output-dir ./fhir_export_demo_all
```

전체 실행 전에는 먼저 한 명을 별도 디렉터리에서 실행합니다.

```bash
python main.py doctor
python main.py export-validate \
  --limit 1 \
  --output-dir ./fhir_export_pre_all
```

`fhir_export_pre_all/manifest.json`에서 `failed_patients=0`,
`invalid_patients=0`이고 `extraction_completeness.requires_limit_review=false`
인지 확인합니다. `true`이면 FHIR 검증에 성공했더라도 하나 이상의 SQL이 행
상한에 도달한 것이므로 추가 원천 행이 있는지 확인해야 합니다. 상한 도달은
보수적인 검토 신호이며 그 자체가 누락을 확정하는 것은 아닙니다.

`--all`은 DB의 모든 환자가 아니라 선택한 source profile의 cohort 파일에
들어 있는 모든 환자를 뜻합니다. 각 key로 DB를 새로 조회하고 LLM SQL과 FHIR
생성을 수행합니다. 중단 후 이어서 실행하려면 같은 출력 경로에 `--resume`을
붙입니다. `--resume`은 Validator와 저장된 task 평가가 모두 통과한 환자만
재사용하며 invalid, failed, task 평가 실패 환자는 다시 생성합니다.

```bash
python main.py export-validate \
  --all \
  --output-dir ./fhir_export_demo_all \
  --resume
```

### 환자 수 제한과 DB 행 제한

서로 다른 세 종류의 제한을 구분해야 합니다.

| 설정 | 제한 대상 | FHIR 생성에 미치는 영향 |
| --- | --- | --- |
| CLI `--limit N` | cohort에서 선택할 환자 수 | 환자 수만 제한 |
| `SOURCE_MAX_*`, `SQL_MAX_ROWS` | 선택 환자의 리소스별 DB 조회 행 | 실제 생성 가능한 리소스 수 제한 |
| 노트북 `DB_ROW_DISPLAY_LIMIT` | 화면에 표시할 DB 행 | 생성·검증에는 영향 없음 |

따라서 `--limit 1`은 임상 행을 한 건만 조회한다는 뜻이 아닙니다. Demo Patient
NDJSON에서 첫 환자 한 명을 선택하고, 그 환자에 대해서는 `.env`의 Encounter와
임상 리소스 상한을 적용해 전체 Pipeline을 실행합니다. 특정 환자를 지정하려면
`--patient-key`를 사용합니다.

현재 설정에서 임상 리소스 SQL의 요청 상한은
`Encounter별 설정 × 실제 선택 Encounter 수`로 계산한 뒤
`SQL_MAX_ROWS=10000`과 비교해 작은 값을 사용합니다. 예를 들어 Encounter가
5건인 환자의 Observation 요청 상한은 `2000 × 5 = 10000`건입니다.

### CLI와 `test.ipynb`의 관계

두 방식은 동일한 `LlmSqlToFhirPipeline`을 사용합니다.

| 구분 | `test.ipynb` | `export-validate` |
| --- | --- | --- |
| 목적 | 단계별 확인·디버깅 | 반복 가능한 생성·검증 배치 |
| 환자 선택 | 노트북의 `PATIENT_KEY` | `--patient-key`, `--limit`, `--all` |
| SQL과 DB 행 | 셀에서 직접 표시 | 환자별 trace JSON에 저장 |
| 단계 제어 | `RUN_*` 플래그 | 전체 Pipeline 자동 실행 |
| 결과 집계 | 셀 출력 중심 | manifest와 validation 파일 |
| 중단 후 재개 | 수동 | `--resume` |

`RUN_RESOURCE_DB_ROWS=True`인 노트북은 실제 Pipeline에 기록용 QueryService를
주입합니다. 따라서 노트북에 표시된 행과 FHIR 생성에 사용된 행은 동일합니다.
노트북에서 20행만 보이더라도 `DB_ROW_DISPLAY_LIMIT=20`에 따른 화면 생략일 수
있으므로 실제 추출 수는 `source_row_counts`와 trace의 `row_count`로 판단합니다.

## 4. SQL 안전성과 Self-Correction

생성 SQL에는 다음 제약이 적용됩니다.

- `SELECT`와 `WITH`만 허용
- DB에서 실제 발견한 schema-qualified relation만 허용
- DDL, DML, row lock, 위험 함수, SQL 주석 차단
- 읽기 전용 transaction과 statement timeout 적용
- 최종 row limit 강제
- 필수 generic alias, 환자/encounter 범위, key 고유성, code 값 검증
- Encounter 및 임상 relation 구조를 DB 실행 전에 검사

실패하면 이전 SQL, DB 오류, 해당 relation의 정확한 metadata를 다시 LLM에
제공합니다. CTE alias와 `alias.column`은 SQL 구조 및 DB metadata로 증명되는
경우에만 보정합니다. 선택 필드가 실제 컬럼으로 입증되지 않으면 임의의 병원별
대체값을 넣지 않고 `NULL`로 낮춥니다.

### 일반 방문·입원 범위 보호

Encounter는 특정 하위 임상 이벤트나 체류가 존재하는 환자만 선택해서는 안
됩니다. 현재 공통 Encounter 질문과 SQL 실행 검증은 다음 규칙을 적용합니다.

- 환자와 직접 연결된 가장 포괄적인 방문·입원 relation을 우선 선택
- 하위 임상 이벤트·처치·측정·위치·세션·체류 존재 여부와 무관하게 조회
- 하위 relation에 대한 `INNER/CROSS JOIN`, `EXISTS`, `IN (SELECT ...)`으로
  Encounter 범위를 축소하면 `result_encounter_scope_narrowing`으로 거부
- 이전 SQL과 동적 PK/FK metadata를 제공해 LLM SQL self-correction
- `_encounter_key`가 비어 있거나 중복되면 fan-out join으로 판단하여 재생성

이 규칙은 특정 테이블이나 컬럼명이 아니라 SQL의 행 축소 의미와 FHIR Encounter
계약만 검사합니다. 따라서 ICU 같은 하위 체류 구조가 없는 병원 DB에도 동일하게
적용됩니다. 원천 DB metadata에 Encounter 의미를 구분할 정보가 전혀 없다면
자동 추론에 한계가 있으므로 PK/FK와 테이블·컬럼 설명을 충실히 제공해야 합니다.

### Encounter 문맥과 의미적 관계 재검사

기존에는 Encounter 조회가 성공해도 후속 임상 SQL이 다른 의미의 진료 키로
필터링하여 0건을 반환하면 정상적인 원천 데이터 부재로 확정될 수 있었습니다.
현재는 다음 동적 문맥을 후속 `Condition`, `Procedure`, `Observation` 질문에
전달합니다.

- 선택된 `_encounter_key` 값
- 실행된 Encounter SQL
- 그 SQL에서 추출한 schema-qualified relation
- DB catalog에서 찾은 선택 relation, 양방향 FK relation 및 구조적으로 관련된
  relation의 PK/FK metadata

실제 테이블명과 컬럼명은 코드·환경변수·profile에 저장하지 않습니다. 실행 SQL과
현재 연결 DB의 catalog에서 매번 계산되며 환자별 trace에 남습니다.

Encounter가 존재하는데 Condition, Procedure 또는 Observation 중 하나가
0건이면 Pipeline은 해당 리소스만 독립적으로 한 번 더 검색합니다. LLM에는 기존
0건 SQL과 Encounter lineage를 제공하고, 임상 relation이 Encounter 키를 직접
가진다고 가정하지 말고 metadata로 증명되는 join 경로를 찾도록 요구합니다.
재조회 과정에서 첫 0건은 self-correction 대상으로 처리하고 두 번째로도
0건이면 `empty_result_confirmed=true`인 정상 원천 데이터 부재로 확정합니다.
확인되지 않은 0건은 `unconfirmed_empty_resources`에 남기고
`task_quality_pass=false`로 처리합니다. FHIR 형식 검증만 통과했더라도 batch
manifest에서는 해당 환자를 `invalid`로 분류하므로 `export-validate` 명령도
실패 상태로 종료됩니다.

```text
Encounter SQL 실행
  → key + relation lineage 생성
  → 후속 리소스 SQL에 동일 문맥 전달
  → 각 임상 리소스 조회가 0건인가?
      ├─ 아니요: 그대로 진행
      └─ 예: 해당 리소스만 관계 기반 RAG 재검색
              ├─ 행 발견: 교정 결과 사용
              └─ 다시 0건: 확인된 원천 데이터 부재로 유지
```

trace와 성능 지표에는 다음 항목이 포함됩니다.

| 항목 | 의미 |
| --- | --- |
| `encounter_context_relations` | Encounter SQL에서 동적으로 추출한 relation |
| `semantic_relationship_retry` | 관계 기반 재검색 trace 여부 |
| `semantic_relationship_rechecks` | 재검색한 리소스 SQL 수 |
| `semantic_relationship_empty_retries` | 재검색 중 0건으로 내부 교정한 횟수 |
| `semantic_relationship_recoveries` | 관계 재검색으로 행을 찾은 리소스 수 |
| `encounter_scope_narrowing_rejections` | 하위 relation으로 범위를 축소해 거부된 SQL 수 |
| `encounter_identity_rejections` | Encounter 키 누락·중복으로 거부된 SQL 수 |
| `clinical_scope_narrowing_rejections` | 복수 임상 relation의 행 축소 JOIN을 실행 전에 거부한 수 |
| `normalized_contract_aliases` | 값 변경 없이 공통 alias의 선행 underscore를 복원한 수 |
| `encounter_qualified_resource_keys` | 입원 간 중복 source key를 Encounter 범위로 한정한 행 수 |
| `requested_row_limit` | 해당 SQL에 실제 적용한 리소스 행 상한 |
| `row_limit_reached` | 결과가 상한에 도달해 추가 원천 행 가능성이 있는지 |
| `row_limit_reached_queries` | 상한에 도달한 SQL trace 수 |
| `empty_result_confirmed` | 두 차례 조회로 실제 0건을 확인했는지 |
| `confirmed_empty_queries` | 확인된 0건 임상 조회 수 |

LLM SQL이 반환하는 공통 alias는
[`source_contract.py`](./fhir_agent_pipeline/source_contract.py)에 정의되어
있습니다. 이 alias는 DB 물리 컬럼 매핑이 아니라 DB와 FHIR 단계 사이의
데이터 계약입니다.

공통 계약에는 `_patient_key`, `_encounter_key`, 리소스별 `_resource_key`만
사용하며 특정 DB의 stay 또는 admission 계층용 alias는 추가하지 않습니다.
LLM이 정확한 값을 반환하면서 공통 alias의 선행 underscore만 생략한 경우에는
재조회하지 않고 결과 컬럼명만 결정적으로 복원합니다. 실제 원천 컬럼명이나 값을
변경하지 않으며, 계약에 없는 이름은 정규화하지 않습니다.
여러 Encounter에서 동일한 임상 source key가 재사용되면 해당 중복 행에 한해
`_encounter_key`와 결합하여 전역적으로 안정적인 `_resource_key`를 만듭니다.
같은 Encounter 안에서도 중복되면 자동으로 숨기지 않고 SQL 결과 오류로 유지합니다.

LLM 질문의 `LIMIT`과 SQL 안전 wrapper는 동일한 `requested_row_limit`을
사용합니다. 리소스별 설정으로 계산한 값이 `SQL_MAX_ROWS`보다 크면 안전 상한을
적용하며, 결과 행 수가 그 상한에 도달하면 `row_limit_reached=true`를 남깁니다.
따라서 제한된 행의 변환 정확도 100%를 전체 원천 데이터 완전성으로 오인하지
않습니다. `--all` 실행 후 이 값이 하나라도 있으면 해당 환자 trace를 확인하고
상한 확대 또는 페이지 처리 후 다시 검증해야 합니다.

## 5. FHIR 생성과 검증

FHIR 생성은 LLM 결과를 그대로 신뢰하지 않습니다.

- FHIR JSON Schema Vector 문맥을 LLM에 제공
- source row 수와 생성 resource 수 일치 확인
- source row별 안정적 ID와 Patient/Encounter 참조 강제
- DB에 없는 code system, unit system, display 제거
- 빈 배열과 근거 없는 meta/profile/extension 제거
- date-only와 timestamp timezone을 구분
- Patient `deceased[x]`, Procedure `performed[x]`, Observation
  `effective[x]`/`value[x]`는 원천 alias가 뒷받침하는 한 종류만 유지
- Observation referenceRange를 원천 alias에서 정규화
- `_필드명`이 실제 FHIR 속성과 직접 일치하면 로컬 FHIR JSON Schema의
  `type`, `items`, `$ref`를 읽어 배열과 complex datatype 외형을 범용 정규화

마지막 규칙은 리소스별 값 목록을 코드에 추가하는 방식이 아닙니다. 예를 들어
SQL 원천의 `_interpretation="abnormal"`은 `Observation.interpretation`의
스키마가 `CodeableConcept[]`임을 실행 시 확인한 뒤
`[{"text": "abnormal"}]`로 바뀝니다. 원천 값이 없으면 LLM이 만든 해당 속성은
제거합니다. 이 과정은 병원 테이블명·컬럼명을 모르며 code/system을 추정하지
않습니다.

검증 계층:

1. 로컬 FHIR R4 JSON Schema
2. Bundle 구조와 필수 리소스
3. ID 중복과 Bundle 내부 reference resolution
4. SQL 원천 행 대비 field/count/code/value/unit/time 정확도
5. HL7 FHIR Validator CLI(설정된 경우)

저장 파일을 독립적으로 다시 검증:

```bash
python main.py validate-export ./fhir_export_run
```

단일 Bundle:

```bash
python main.py validate ./fhir_export_run/patient-123.bundle.json
```

## 성능평가 지표

| 평가 영역 | 핵심 지표 | 의미 |
| --- | --- | --- |
| FHIR 표준 적합성 | FHIR Validation Pass Rate | JSON Schema와 외부 Validator 통과율 |
| 원천 충실도 | Source Field Accuracy | SQL 조회값이 FHIR에 보존된 비율 |
| 생성 정확도 | Resource Count Alignment | SQL 행 수 대비 누락·과생성 |
| 추출 완전성 | Row Limit Review | SQL이 설정 상한에 도달했는지 |
| Patient/Encounter | Field-level Accuracy | key, 기간, 상태, 참조 정확도 |
| Condition/Procedure | Precision, Recall, F1 | stable source key 기준 생성 탐지 |
| Observation | Detection F1 + Value/Unit/Time Accuracy | 이벤트와 값·단위·시간 정확도 |
| 참조 무결성 | Reference Resolution Rate | Bundle 내부 참조 해석 비율 |
| 코드 품질 | Coding Accuracy | DB가 실제 제공한 system/code 보존 |
| 자동 교정 | Repair Success Rate | SQL/FHIR 재시도 후 복구 비율 |
| RAG 효과 | RAG Ablation Improvement | 동일 cohort의 RAG 전후 차이 |

RAG 비교:

```bash
python main.py compare-rag baseline/manifest.json rag/manifest.json
```

매핑이 제거됐으므로 원천에 code system 또는 UCUM 정보가 없을 때
terminology system coverage는 낮을 수 있습니다. 이를 임의 URI로 채우지 않는
것이 현재 정책입니다.

`Resource Count Alignment=100%`는 조회된 SQL 행이 모두 FHIR로 생성됐다는
뜻입니다. 설정 상한 이전의 DB 전체 행까지 모두 추출됐다는 뜻은 아닙니다.
전체 완전성 판정에는 manifest의 `extraction_completeness`와 환자별 trace의
`requested_row_limit`, `row_limit_reached`, `row_count`를 함께 사용합니다.
예를 들어 원천 검사 이벤트가 800건 이상인데 SQL 상한 200으로 Observation
200건을 생성·검증했다면 count alignment와 Validator가 모두 100%여도 전체
추출 완전성은 확인되지 않은 상태입니다. 현재 `.env`는 이 사례를 위해 리소스별
SQL 안전 상한을 10,000건으로 높였으며, 다시 상한에 닿으면 manifest에 검토
필요 상태를 남깁니다.

## 기록된 검증 기준선

아래 결과는 2026-07-26 당시 `.env`와 `gemma4:e4b`로 demo Patient NDJSON 앞
10명을 실행한 기준선입니다. Demo 파일은 대상 key를 정하는 데만 사용했고 임상
내용은 각 key로 DB를 조회해 새로 생성했습니다. Encounter lineage 기반 의미적
재검사와 현재 행 상한 추적 도입 이전 결과이므로 현재 코드의 성능 결과로
해석하거나 새 실행과 혼합하지 않습니다.

```bash
python main.py export-validate \
  --limit 10 \
  --output-dir ./fhir_export_demo_generic_10

python main.py validate-export ./fhir_export_demo_generic_10
```

결과:

| 항목 | 결과 |
| --- | ---: |
| 대상 / valid / invalid / pipeline 실패 | 10 / 10 / 0 / 0 |
| FHIR Validator fatal/error | 0 / 0 |
| FHIR Validator warning | 478 |
| 총 리소스 | 163 |
| Patient / Encounter | 10 / 10 |
| Condition / Procedure / Observation | 60 / 13 / 70 |
| Resource Count Alignment | 163 / 163 (100%) |
| Condition/Procedure/Observation F1 | 100% / 100% / 100% |
| Observation value/unit/time accuracy | 100% / 100% / 100% |
| Reference Resolution | 100% |
| Coding Accuracy | 100% |
| Source Field Accuracy | 988 / 988 (100%) |
| FHIR Validation Pass Rate | 100% |
| Strict End-to-End Pass Rate | 100% |
| FHIR 생성 first-pass / repair attempts | 97.89% / 2회 |

Validator warning은 주로 원천 DB에 code-system URI가 없어서 system을 생략한
경우, 미래 연도, narrative/performer 권고입니다. error가 아니며 임의 매핑으로
숨기지 않습니다.

### 변경 후 단일 환자 smoke test

2026-07-27에 Vector DB를 새 profile fingerprint로 재구축하고 demo cohort 첫
환자를 전체 5종 리소스로 다시 실행했습니다. smoke test에서는 실행 명령의
환경변수로 Encounter와 Encounter별 임상 리소스 상한을 각각 10건으로 제한했으며,
`.env`의 운영 상한은 변경하지 않았습니다.

| 항목 | 결과 |
| --- | ---: |
| valid / invalid / pipeline 실패 | 1 / 0 / 0 |
| Patient / Encounter | 1 / 5 |
| Condition / Procedure / Observation | 50 / 1 / 50 |
| 총 리소스와 count alignment | 107 / 100% |
| Source Field / Reference / Coding Accuracy | 100% / 100% / 100% |
| Observation value/unit/time accuracy | 100% / 100% / 100% |
| FHIR Validator fatal/error/warning | 0 / 0 / 319 |
| RAG 사용 SQL | 5 / 5 |
| SQL 실행 오류 후 복구 | 1회 |
| 정규화한 공통 alias / Encounter 한정 key | 6 / 34 |

Encounter SQL은 하위 체류 relation을 조인하지 않고 포괄적인 방문·입원
relation 하나에서 일반 입원 5건을 조회했습니다. 그 실행 relation이
`encounter_context_relations`로 Condition, Procedure, Observation에 전달됐고
세 리소스 모두 같은 Encounter 범위에서 생성됐습니다. 실제 relation 이름은
현재 DB metadata에서 나온 실행 결과이며 코드나 profile 매핑이 아닙니다.

결과 디렉터리는 Git 추적 대상이 아니며 현재 저장소에는 포함되어 있지 않습니다.
새 실행에서는 명시한 `--output-dir` 아래에 manifest, 환자별 Bundle,
validation, trace가 생성됩니다.

### FHIR Schema 기반 정규화 재검증

2026-07-27에 기존 노트북에서 발생한
`Observation.interpretation is not of type array` 오류를 위 범용 규칙으로
수정하고, 동일한 실제 DB→RAG SQL→LLM FHIR→Validator 경로를 단일 환자로
재실행했습니다. 실행 시간만 줄이기 위해 명령 단위로 Encounter 및 임상
리소스 상한을 2건으로 제한했으며 `.env`는 변경하지 않았습니다.

| 항목 | 결과 |
| --- | ---: |
| valid / invalid / pipeline 실패 | 1 / 0 / 0 |
| Patient / Encounter / Condition / Procedure / Observation | 1 / 2 / 4 / 0 / 4 |
| 총 리소스와 count alignment | 11 / 100% |
| JSON Schema / FHIR Validator pass | 100% / 100% |
| Validator fatal / error / warning | 0 / 0 / 31 |
| Source Field / Reference / Coding Accuracy | 100% / 100% / 100% |
| SQL self-correction attempts | 3회 |

Procedure 원천 행 0건은 실패가 아니라 해당 환자·조회 범위에 처치 행이 없는
정상 결과로 처리됐습니다.

### 임상 JOIN 사전 검사 재검증

2026-07-27에 Observation SQL이 raw/derived 임상 relation을 `INNER JOIN`하여
원천 행을 제거하던 문제를 수정했습니다. 특정 schema나 table을 제외하지 않고,
Encounter lineage 밖에서 행을 생성하는 임상 relation이 둘 이상이면 DB 실행
전에 SQL을 거부합니다. 대표 event relation 하나와 선택적 `LEFT JOIN` 보조
relation 하나만 허용합니다.

동일 demo 환자를 명령 단위 상한으로 다시 검증한 결과:

| 항목 | 결과 |
| --- | ---: |
| valid / invalid / pipeline 실패 | 1 / 0 / 0 |
| Patient / Encounter / Condition / Procedure / Observation | 1 / 5 / 50 / 1 / 50 |
| Observation SQL 행 / FHIR 리소스 | 50 / 50 |
| Task quality pass | true |
| 확인되지 않은 0건 리소스 | 0 |
| FHIR Validator fatal / error / warning | 0 / 0 / 319 |

실행된 Observation SQL은 대표 event relation과 동적으로 선택된 Encounter
lineage만 결합했습니다. 실제 relation 이름은 DB metadata와 RAG 실행 결과이며
코드에 저장된 병원별 매핑이 아닙니다.

## 결과 디렉터리 관리

`fhir_export*` 결과 디렉터리는 `.gitignore` 대상입니다. 기준선, 사전검증,
전체실행을 서로 다른 디렉터리에 저장하고 같은 실행을 이어갈 때만 `--resume`을
사용합니다.

결과 파일의 의미:

| 파일 | 내용 |
| --- | --- |
| `manifest.json` | 생성 직후 cohort 집계와 성능평가 |
| `revalidation.json` | 저장된 Bundle의 독립 재검증 집계 |
| `patient-<key>.bundle.json` | 환자별 FHIR Bundle |
| `patient-<key>.validation.json` | 환자별 Validator 및 세부 지표 |
| `patient-<key>.trace.json` | LLM SQL/FHIR 호출, 교정, RAG 근거 |

새 실험은 목적이 드러나는 별도 출력 경로를 사용합니다.

```bash
python main.py export-validate \
  --limit 10 \
  --output-dir ./fhir_export_demo_generic_10
```

동일 실행을 이어갈 때는 새 디렉터리를 만들지 않고 `--resume`을 사용합니다.

```bash
python main.py export-validate \
  --limit 10 \
  --output-dir ./fhir_export_demo_generic_10 \
  --resume
```

## 다른 병원 DB 적용

1. `.env`의 `SOURCE_DB_URL`을 새 DB로 변경합니다.
2. 직접 patient key로 실행한다면 `profiles/hospital_sample/profile.json`처럼
   cohort provider를 `none`으로 둡니다.
3. Patient NDJSON cohort가 있다면 generic profile의 파일 패턴과 identifier
   선택 규칙만 수정합니다.
4. `python main.py doctor`로 발견 테이블을 확인합니다.
5. `python main.py build-vectors`로 Vector DB를 갱신합니다.
6. 소수 환자로 `export-validate`를 실행한 뒤 cohort 전체로 확대합니다.

새 DB의 테이블 매핑 JSON이나 Python mapper를 만들 필요는 없습니다. 단,
스키마/컬럼 이름만으로 의미를 판단하기 어려운 DB는 테이블·컬럼 comment와
PK/FK를 충실히 정의할수록 Vector 검색과 SQL 생성 품질이 좋아집니다.

## 테스트

```bash
python -m pytest -q
python -m compileall -q fhir_agent_pipeline main.py
```

현재 회귀 테스트는 61건이며 모두 통과합니다. 임의 이름의 source relation에서
lineage가 동적으로 추출되는지, 모든 선택 임상 조회가 0건일 때 관계 재검색이
수행되고 복구 결과가 최종 FHIR 입력으로 교체되는지, FK로만 연결된 relation도
동적 schema hint에 포함되는지를 검사합니다. 하위 relation의 내부 조인과 존재
하위쿼리 차단, Encounter key 중복 차단, 알려진 공통 alias만 대상으로 하는
선행 underscore 복구, 입원 간 중복 resource key 한정도 포함합니다.

단계별 수동·통합 테스트는 [`test.ipynb`](./test.ipynb)를 사용합니다. 노트북은
설정, demo 환자 key, DB catalog, Vector RAG, LLM SQL, 단일 환자 FHIR,
Validator, 선택적 10명 배치 순서로 구성되어 있습니다. LLM 호출이나 Vector
재구축처럼 시간이 오래 걸리는 단계는 첫 번째 설정 셀의 `RUN_*` 플래그로
실행 여부를 제어합니다. `RUN_RESOURCE_DB_ROWS=True`로 설정하면 선택 환자에 대해
Patient, Encounter, Condition, Procedure, Observation별 생성 SQL과 실제 DB
반환 행을 표로 확인할 수 있습니다. 원천 임상값이 표시되므로 승인된 환경에서만
사용하고 `DB_ROW_DISPLAY_LIMIT`으로 표시 범위를 제한합니다. 기록용
QueryService를 실제 Pipeline에 주입하므로 화면에 표시되는 행과 FHIR 생성에
사용되는 행은 동일한 한 번의 실행 결과입니다. 리소스별 출력의
`encounter_context_relations`와 `semantic_relationship_retry`, 마지막
요약의 `semantic_relationship_rechecks`와
`semantic_relationship_recoveries`, `clinical_scope_narrowing_rejections`,
`confirmed_empty_queries`, `row_limit_reached_queries`로 관계 교정, 0건 확인,
추출 상한 도달 여부를 확인할 수 있습니다. Pipeline 실행을 시도했다가 실패하면
후속 Validator 셀은 이전 Bundle로 대체 검증하지 않고 오류를 표시합니다.

DB metadata, 공통 SQL 질문 profile, FHIR JSON Schema, embedding 모델 또는
차원이 변경되면 Vector DB를 갱신합니다. CLI 설명, 검증 집계 또는 FHIR
후처리 코드만 변경된 경우에는 Vector 문서가 바뀌지 않으므로 재구축할 필요가
없습니다.

```bash
python main.py build-vectors
python main.py inspect-vectors
```
