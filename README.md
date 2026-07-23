# MIMIC-IV DB → LLM → FHIR

이 프로젝트의 단일 목적은 별도 PostgreSQL 서버의 MIMIC-IV 데이터를 LLM으로 조회하고,
조회 결과를 FHIR R4 리소스로 변환한 뒤 검증하는 것입니다.

지원 리소스는 `Patient`, `Encounter`, `Condition`, `Procedure`, `Observation`입니다.

## 처리 흐름

```text
DB Schema + FHIR Schema/매핑
             │
             ▼
       PostgreSQL pgvector
             │
환자 ID ──> LLM SQL 생성 ──> SQL 안전 검사 ──> 읽기 전용 DB 조회
                                                │
                                                ▼
                          LLM FHIR JSON 생성 및 자동 교정
                                                │
                                                ▼
                          Bundle 생성 ──> Schema/참조/HL7 검증
```

결정적 Builder, Mapping Advisor, 기존 FHIR NDJSON benchmark 경로는 제거했습니다.
`convert`와 `export-validate`는 모두 같은 `LlmSqlToFhirPipeline`을 사용합니다.

## 현재 구현 내용

### 핵심 실행 경로

`LlmSqlToFhirPipeline.run()`은 다음 순서로 동작합니다.

1. `.env`와 `db/mimic_mapping_config.json`을 읽고 설정값·리소스 의존성을 검사합니다.
2. SQL/FHIR Vector 테이블의 존재 여부, 필수 컬럼, 행 수를 검사합니다.
3. `ResourceAdapter` 순서에 따라 Patient → Encounter → Condition → Procedure → Observation을 처리합니다.
4. SQL VectorDB 문맥으로 리소스별 PostgreSQL `SELECT`를 생성합니다.
5. `SqlSafetyPolicy`가 SQL을 검사하고 `SafeSqlGateway`가 읽기 전용 트랜잭션에서 실행합니다.
6. SQL 결과가 요청한 `subject_id`와 선택된 `hadm_id` 범위 안인지 확인합니다.
7. DB `CHAR` 패딩을 제거하고 ICD 코드·system 등 LLM 입력용 소스 값을 정규화합니다.
8. FHIR Schema/매핑 VectorDB 문맥, 소스 행, ID·참조 계약으로 FHIR JSON을 생성합니다.
9. 실패한 FHIR 배치에는 이전 출력과 오류를 다시 전달해 설정 횟수만큼 자동 교정합니다.
10. 생성 리소스를 DB 조회 행과 결정적 ID로 다시 대응시켜 값 정확도, 리소스 F1, 참조와 코딩 정확도를 계산합니다.
11. Bundle로 조립하고 공식 Schema, 의미 규칙, 참조, 품질 지표와 HL7 Validator를 실행합니다.
12. DB 엔진을 정리하고 Bundle, 검증 보고서와 LLM trace를 저장합니다.

운영 기본값인 `VECTORS_ENABLED=true`에서는 VectorDB가 없거나 호환되지 않으면
LLM 변환을 시작하지 않고 `build-vectors` 실행을 안내합니다. `false`는 RAG 비교
실험의 LLM-only baseline을 만들 때만 사용합니다.
또한 Condition, Procedure, Observation은 먼저 조회한 Encounter ID 범위 안에서만 생성됩니다.

### 리소스별 현재 매핑

| FHIR 리소스 | 기본 DB 소스 | 주요 변환 정책 |
|---|---|---|
| Patient | `patients` | `subject_id` 기반 ID, 외부 `gender_map`, 조회 근거 없는 `birthDate` 차단 |
| Encounter | `admissions` | `hadm_id` 기반 ID, admission type별 class, 퇴원 시간 기반 status와 period |
| Condition | `diagnoses_icd` + `d_icd_diagnoses` | 입원 ID와 sequence 기반 ID, ICD-9/10 system·소수점, Patient/Encounter 참조 |
| Procedure | `procedures_icd` + `d_icd_procedures` | 입원 ID와 sequence 기반 ID, ICD-9/10, performed date, Patient/Encounter 참조 |
| Observation | `labevents` | event/row/item ID 후보, 검사값·단위·참고범위·flag, UCUM, Patient/Encounter 참조 |

### 대상 리소스 선정 이유

이 5종은 `Patient → Encounter → Condition/Procedure/Observation`으로 이어지는 환자 중심
임상 흐름을 FHIR Bundle로 표현하기 위한 초기 최소 범위입니다.

- MIMIC-IV 원천 테이블과 `subject_id`, `hadm_id`, ICD code, lab event ID 등 대응 키가 명확함
- 환자·입원·진단·처치·검사 간 참조 관계와 Bundle 무결성을 함께 검증할 수 있음
- DB 원본을 기준으로 필드 정확도, Precision/Recall/F1, 값·단위·시간 정확도를 계산할 수 있음
- 식별자, 시간, 코드, 수치, UCUM 단위와 참조 등 주요 FHIR 변환 유형을 고르게 포함함

Medication 관련 리소스는 후속 확장 대상으로 둡니다. MIMIC-IV의 처방·조제·실제 투여·ICU
주입 데이터는 의미가 서로 다르고, FHIR에서도 `MedicationRequest`,
`MedicationAdministration`, `MedicationDispense` 등으로 목적별 모델이 나뉩니다. 약물명,
용량, 단위, 경로와 RxNorm 표준화 기준을 먼저 정의해야 하므로 초기 검증 범위에서는 제외했습니다.

실제 테이블명과 컬럼명은 `.env` 및 매핑 JSON에서 변경할 수 있습니다. ID는 코드에서 문자열을
조합하지 않고 `resource_ids`의 `template` 또는 `candidates` 정책으로 생성합니다.

### LLM SQL 생성과 안전 실행

`NaturalLanguageSqlAgent`는 SQL VectorDB에서 질문과 관련된 스키마 문서를 검색합니다. 생성 SQL이
실패하면 PostgreSQL 오류를 다음 요청에 전달하며 `LLM_SQL_MAX_ATTEMPTS` 범위에서 교정합니다.

실행 단계에서는 다음을 코드로 강제합니다.

- 문자열 리터럴을 제외한 다중 statement와 SQL 주석 차단
- `INSERT`, `UPDATE`, `DELETE`, DDL, 권한·관리 명령 차단
- `FOR UPDATE` 등 행 잠금 차단
- 서버 파일, 지연, 외부 연결 함수 차단
- `SQL_ALLOWED_SCHEMAS`, `SQL_ALLOWED_TABLES` 이외의 접근 차단
- `SET TRANSACTION READ ONLY`, statement timeout, 최종 `LIMIT` 적용

LLM이 올바른 SQL을 만들었더라도 결과 행의 환자 또는 입원 범위가 다르면 FHIR 생성으로 넘기지 않습니다.

### LLM FHIR 생성과 자동 교정

`FhirLlmResourceService`는 `LLM_FHIR_BATCH_SIZE` 단위로 소스 행을 처리하며 각 행에
0부터 시작하는 `source_row`를 요구합니다. 다음 조건을 모두 만족해야 배치가 채택됩니다.

- 소스 행 수와 생성 리소스 수가 정확히 일치
- `source_row` 누락·중복 없음
- 매핑 정책으로 계산한 ID와 일치
- Patient 및 Encounter 참조가 선택 범위와 일치
- Patient gender, ICD system/code, UCUM code가 소스·매핑과 일치
- 빈 배열이 없고 현재 Task 필수 필드가 존재
- 공식 FHIR R4 JSON Schema 통과

실패 시 검증 오류와 이전 JSON을 LLM에 제공해 `LLM_FHIR_MAX_ATTEMPTS`까지 교정합니다.
모든 시도가 실패하면 해당 리소스 유형에서 즉시 중단하며 불완전한 Bundle을 저장하지 않습니다.

### Bundle과 trace

Bundle의 `fullUrl`은 `resourceType/id`에서 안정적으로 생성한 UUID이며, 내부의
`Patient/{id}`, `Encounter/{id}` 참조는 해당 `urn:uuid:`로 변환합니다. 동일한
`resourceType/id`가 두 번 나오면 Bundle 생성 단계에서 거부합니다.

`bundle.trace.json`에는 다음 정보가 들어갑니다.

- 실행 모드 `llm-sql-and-llm-fhir`
- 전체 및 리소스별 LLM 호출 횟수
- 리소스별 DB 조회 행 수
- 리소스별 질문, 최종 안전 SQL, 설명, SQL 교정 호출 수
- SQL 초회 성공률, 안전 정책 거부 수, DB 실행 오류 수
- FHIR 배치 초회 성공률, 자동 교정 성공률, 교정 시도와 오류 감소량
- DB 원본 대비 필드 정확도, 리소스 F1, 참조와 용어 정확도

원본 DB 비밀번호나 전체 Vector 임베딩 값은 trace에 기록하지 않습니다.

## 설치

Python 3.10 이상, PostgreSQL과 pgvector, Ollama가 필요합니다.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

ollama pull gemma4:e4b
ollama pull nomic-embed-text
```

기존 `.env`가 있다면 덮어쓰지 말고 [.env.example](./.env.example)과 키만 맞추세요.
실제 DB 비밀번호가 들어 있는 `.env`는 Git에 올리지 않습니다.

## 환경 설정

먼저 다음 값을 실제 환경에 맞게 수정합니다.

```dotenv
MIMIC_DB_URL=postgresql+psycopg2://사용자:비밀번호@DB서버:5432/mimiciv-2.2
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=gemma4:e4b
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIMENSION=768

FHIR_SCHEMA_PATH=./db/fhir.schema.json
MIMIC_MAPPING_CONFIG=./db/mimic_mapping_config.json
MIMIC_FHIR_DATA_PATH=/home/mdhi/physionet.org/files/mimic-iv-fhir-demo
FHIR_DEFAULT_TIMEZONE=Z
```

주요 실행 정책:

| 설정 | 의미 | 예시 |
|---|---|---:|
| `PATIENT_SUBJECT_ID` | 명령행에서 생략할 기본 환자 ID | 빈 값 |
| `FHIR_OUTPUT_RESOURCES` | 생성할 리소스 | 5종 전체 |
| `MIMIC_MAX_ENCOUNTERS` | 환자당 최근 입원 수 | `1` |
| `MIMIC_MAX_CONDITIONS_PER_ENCOUNTER` | 입원당 진단 수 | `10` |
| `MIMIC_MAX_PROCEDURES_PER_ENCOUNTER` | 입원당 처치 수 | `10` |
| `MIMIC_MAX_OBSERVATIONS_PER_ENCOUNTER` | 입원당 검사 수 | `10` |
| `SQL_MAX_ROWS` | 안전 Gateway의 최종 행 제한 | `100` |
| `LLM_SQL_MAX_ATTEMPTS` | SQL 자동 교정 포함 최대 호출 | `2` |
| `LLM_FHIR_MAX_ATTEMPTS` | FHIR 배치 자동 교정 포함 최대 호출 | `2` |
| `LLM_FHIR_BATCH_SIZE` | FHIR LLM 호출당 소스 행 수 | `2` |
| `LLM_FHIR_CONTEXT_SEARCH_K` | FHIR Vector 문맥 수 | `20` |
| `FHIR_VALIDATOR_JAR` | HL7 Validator CLI jar | 선택 사항 |

소스 컬럼, 성별, Encounter class/status, ID 생성, ICD와 UCUM 규칙은
[mimic_mapping_config.json](./db/mimic_mapping_config.json)에서 변경합니다.

## 실행

### 1. 연결 확인

```bash
python main.py doctor
```

FHIR Schema, 매핑 설정, 별도 DB 서버, Ollama 설정을 확인합니다.

### 2. VectorDB 구축

```bash
python main.py build-vectors
python main.py inspect-vectors
```

| 기본 테이블 | 내용 |
|---|---|
| `public.fhir_mapping_vectors` | FHIR R4 JSON Schema와 MIMIC→FHIR 매핑 정책 |
| `public.mimic_sql_schema_vectors` | 허용된 실제 DB 테이블·컬럼·PK·FK |

문서 `content_hash`가 바뀐 항목만 다시 임베딩하며 cosine 검색용 HNSW 인덱스를 만듭니다.

### 3. 자연어 SQL 확인

```bash
python main.py ask "환자 10000032의 최근 입원을 보여줘"
```

LLM SQL은 실행 전에 다음 정책을 통과해야 합니다.

- `SELECT` 또는 `WITH ... SELECT` 한 문장
- 허용된 스키마와 테이블만 접근
- DDL, DML, 행 잠금, 서버 파일·외부 연결 함수 차단
- 읽기 전용 트랜잭션과 statement timeout
- 전체 SQL을 감싼 `SQL_MAX_ROWS` 강제 제한

### 4. 환자 한 명 변환 및 검증

```bash
python main.py convert --subject-id 10000032 --hadm-id 25742920
```

`--hadm-id`는 선택 사항입니다. 생략하면 설정된 개수만큼 최근 입원을 조회합니다.

생성 파일:

- `fhir_output/bundle.json`: 최종 FHIR Bundle
- `fhir_output/bundle.validation.json`: 생성 직후 검증 결과
- `fhir_output/bundle.trace.json`: LLM SQL, 조회 행 수와 호출 횟수
- `fhir_output/refactor.revalidation.json`: 별도 프로세스의 재검증 결과

### 5. 저장 Bundle 독립 재검증

```bash
python main.py validate fhir_output/bundle.json
```

### 6. 여러 환자 순차 변환

```bash
python main.py export-validate --subject-id 10000032
python main.py export-validate --subject-id 10000032 --subject-id 10000048
python main.py export-validate --limit 10
python main.py export-validate --limit 10 --resume
python main.py export-validate --all --resume
```

`--limit`과 `--all`의 환자 목록은 DB 전체 환자가 아니라 `MIMIC_FHIR_DATA_PATH` 아래
최신 버전의 `fhir/MimicPatient.ndjson.gz`에서 읽습니다. demo Patient의 FHIR `id`는 UUID이므로
DB 조회에는 `identifier[].value`의 숫자 `subject_id`를 사용합니다.

- `--limit 10`: demo Patient NDJSON 순서의 앞 10명을 DB→LLM→FHIR로 변환
- `--all`: demo Patient NDJSON에 포함된 모든 환자를 변환
- `--subject-id`: demo cohort와 관계없이 명시한 환자만 변환
- `--resume`: Bundle과 검증 파일이 모두 있는 환자는 LLM 생성을 건너뜀

현재 제공된 demo Patient NDJSON에는 100명이 있습니다. 각 환자의 임상 내용은 demo FHIR 파일을
그대로 복사하는 것이 아니라, 추출한 `subject_id`로 별도 DB를 조회하여 LLM이 새로 생성합니다.
`manifest.json`의 `cohort`에는 demo Patient 파일 경로와 적용한 limit이 기록됩니다.
현재 연결된 MIMIC DB와 대조한 결과 demo의 100개 `subject_id`가 모두 존재합니다.

```bash
python main.py validate-export fhir_export
```

## 검증 항목

현재 Task의 대표 성능평가 지표는 다음 10개 영역으로 구성합니다.

| 평가 영역 | 핵심 지표 | 주 용도 |
|---|---|---|
| FHIR 표준 적합성 | FHIR Validation Pass Rate | HL7 Validator와 Schema 표준 준수 |
| 원천 충실도 | Source Field Accuracy | DB 조회 값 보존 |
| 리소스 생성 정확도 | Resource Count Alignment | 원천 행 대비 누락·과생성 확인 |
| Patient/Encounter | Field-level Accuracy | 단일 리소스 필드 매핑 |
| Condition/Procedure | Precision, Recall, F1 | 다중 행 리소스 탐지 |
| Observation | Detection F1 + Value/Unit Accuracy | 검사 행·값·단위 변환 |
| 참조 무결성 | Reference Resolution Rate | Bundle 내부 연결성 |
| 코드 품질 | Coding Accuracy | ICD/UCUM system과 code 매핑 |
| 자동 교정 | Repair Success Rate | validation-guided self-correction 효과 |
| RAG 효과 | RAG Ablation Improvement | LLM-only 대비 VectorDB 기여도 |

`bundle.validation.json`에는 두 수준의 결과가 기록됩니다.

- `metrics.performance_evaluation`: 위 표의 대표 지표만 용도별로 정리한 결과
- `metrics.task_evaluation`: 평가 분자·분모와 리소스별 상세 결과

대표 결과에는 `overall_task_pass`, `fhir_standard_conformance`,
`source_fidelity`, `resource_generation_accuracy`, `patient_encounter`,
`condition_procedure`, `observation`, `reference_integrity`, `coding_quality`,
`self_correction`, `rag_effect`가 포함됩니다.

상세 결과의 주요 항목:

- `resource_count_alignment`: 기대/생성 개수, 차이, 일치율
- `resource_identity_metrics`: 리소스별 TP/FP/FN과 precision/recall/F1
- `observation_quality`: Detection F1, 값·단위·시각 정확도
- `unsupported_top_level_fields`: 현재 조회·매핑 계약으로 설명되지 않는 필드
- `generation`: 리소스별 FHIR 배치 초회 성공 수, repair 성공 수와 교정 횟수
- `performance_evaluation.self_correction`: 전체 배치 초회 성공률, repair 성공률과 평균 교정 횟수
- `sql`: SQL 초회 성공률, 응답 형식 오류, 안전 정책 거부, DB 실행 오류

다환자 `manifest.json`도 `performance_evaluation`에 같은 구조의 cohort 지표를
제공합니다. Accuracy는 환자별 비율의 단순 평균이 아니라 `correct/evaluated`를
합산하며, F1은 TP/FP/FN을 합산하여 다시 계산합니다.

분모가 0인 정확도는 “평가 대상 오류가 없음”을 뜻하도록 `1.0`으로 표시하며,
각 지표에 `evaluated` 수를 같이 제공합니다. 예를 들어 UCUM 단위가 없는 환자의
`ucum_code.accuracy=1.0`, `evaluated=0`은 UCUM 변환이 실제로 시험됐다는 뜻이 아닙니다.

FHIR Validator는 구조와 표준 준수를 검사하지만 값의 DB 출처는 판단하지 못합니다.
따라서 LLM 생성 단계의 강제 검사와 위의 DB 원본 기반 사후 평가를 함께 봐야 합니다.
저장 Bundle만 `validate`하면 원본 DB 행이 없으므로 Task 평가는 새로 계산할 수 없습니다.
`convert` 또는 `export-validate`로 생성할 때 계산된 Task 평가는 validation 파일에 보존되며,
`validate-export` 재검증도 기존 값을 유지합니다.

### 해석상의 한계

- F1의 정답 key는 `mimic_mapping_config.json`의 결정적 resource ID입니다. 임상적으로 같은
  사건을 다른 ID로 합치는 의미론적 중복 평가는 아직 포함하지 않습니다.
- `unsupported_top_level_fields`는 현재 조회·매핑 계약 밖의 최상위 속성을 찾습니다.
  모든 중첩 값의 임상적 환각을 판정하는 지표는 아닙니다.
- SQL 안전 통과율은 코드의 allowlist/read-only 정책에 대한 통과율이지 SQL의 임상적 정답률이
  아닙니다. 조회 범위와 반환 컬럼은 별도로 검사합니다.
- 단일 실행의 `rag_ablation_improvement`는 `null`입니다. 같은 cohort와 생성 설정으로
  LLM-only와 LLM+RAG를 각각 실행한 후에만 개선율을 계산합니다.

### RAG ablation 비교

두 실행은 출력 디렉터리 외의 환자 범위, 모델, 배치 크기, 시드 성격의 조건을
동일하게 유지해야 합니다.

```bash
VECTORS_ENABLED=false FHIR_EXPORT_DIR=./fhir_export_no_rag \
  python main.py export-validate --limit 10

VECTORS_ENABLED=true FHIR_EXPORT_DIR=./fhir_export_rag \
  python main.py export-validate --limit 10

python main.py compare-rag \
  fhir_export_no_rag/manifest.json \
  fhir_export_rag/manifest.json
```

`compare-rag`는 지표별 `LLM+RAG - LLM-only` 절대 개선량과, 품질지표
개선량의 평균인 `rag_ablation_improvement`를 출력합니다.

매핑 계약에 포함되지 않은 최상위 필드 목록은 코드에 중복 선언하지 않고
`db/mimic_mapping_config.json`의 `evaluation.allowed_top_level_fields`에서 관리합니다.

## 코드 구조

```text
main.py                                  CLI 진입점
fhir_agent_pipeline/cli.py               명령 파싱과 결과 저장
fhir_agent_pipeline/config.py            .env 설정과 범위 검증
fhir_agent_pipeline/db.py                DB 엔진과 안전한 PostgreSQL 식별자
fhir_agent_pipeline/vector_store.py      FHIR/SQL pgvector 구축과 검색
fhir_agent_pipeline/sql_agent.py         LLM SQL 생성과 읽기 전용 Gateway
fhir_agent_pipeline/llm_query_service.py 리소스별 SQL 질문·범위·소스 정규화
fhir_agent_pipeline/agents.py            LLM FHIR JSON 호출
fhir_agent_pipeline/llm_resource_service.py FHIR 계약·교정·소스 충실도
fhir_agent_pipeline/llm_pipeline.py      전체 단계 오케스트레이션
fhir_agent_pipeline/fhir_models.py       Bundle 조립과 내부 참조 변환
fhir_agent_pipeline/fhir_schema.py       공식 R4 Schema 캐시
fhir_agent_pipeline/validation.py        Schema·참조·품질·HL7 검증
fhir_agent_pipeline/evaluation.py        DB 원본 충실도·리소스 F1·참조·코딩 평가
fhir_agent_pipeline/performance.py       대표 지표 통합·cohort 집계·RAG 비교
fhir_agent_pipeline/export_service.py    demo cohort 선택과 동일 LLM Pipeline의 다환자 실행
db/mimic_mapping_config.json             외부 매핑 정책
tests/                                   DB/Ollama 없이 실행하는 회귀 테스트
```

## 리팩터링 결과

제거한 legacy/중복 구현:

- `pipeline.py`: 결정적 변환 Pipeline
- `normalization.py`: Builder와 Builder를 상속한 Normalizer
- `MappingAgent`와 Mapping Advisor 프롬프트
- `--deterministic`, `--with-llm-advisor`, `benchmark` 명령
- 직접 DB→Builder 추출 모델과 조회 함수
- `PatientDemoRow`, `MultiAgentFhirPipeline` 등 호환 alias
- 루트의 중복 `config.py`와 구형 환경 예제
- 리소스별 Pydantic 클래스는 제거하고, JSON Schema 누락 필수값은 `ResourceAdapter`의 공통 규칙으로 통합
- 세 군데에 있던 PostgreSQL 식별자 검사 구현

현재 모든 생성 경로는 `LlmSqlToFhirPipeline` 하나로 수렴합니다.

## 테스트 및 최근 실제 검증

```bash
pytest -q
```

2026-07-23 실제 환경에서 `gemma4:e4b`로 환자 `10000032`, 입원 `25742920`의
DB 조회 → LLM FHIR 생성 → 자동 교정 → HL7 Validator → 독립 재검증을 확인했습니다.

| 항목 | 결과 |
|---|---:|
| VectorDB | FHIR 11,503 / SQL 10 |
| 조회·생성 리소스 | Patient 1 / Encounter 1 / Condition 10 / Procedure 1 / Observation 10 |
| 전체 리소스 | 23 |
| 전체 LLM 호출 | 20 (SQL 6 / FHIR 14) |
| DB 원본 필드 정확도 | 256/256 (`1.0`) |
| Condition/Procedure/Observation macro F1 | `1.0` |
| 논리 참조 정확도 | 43/43 (`1.0`) |
| coding system/code 정확도 | 42/42 (`1.0`) |
| 근거 없는 최상위 필드 | 0 |
| SQL 초회 성공 / 안전 통과 | `0.8` / `1.0` |
| FHIR 자동 교정 | 1회, 최종 성공 |
| HL7 Validator | fatal 0 / error 0 / warning 60 |
| 독립 재검증 | `valid: true` |

HL7 warning은 프로파일·권장 표현 관련 경고이며 fatal/error는 없었습니다. 이 실제 실행에서
기존 구조 검증만으로 발견하지 못했던 Condition/Procedure `identifier.value` 불일치를 새
원본 충실도 평가가 검출했고, source row별 기대 ID를 생성 계약과 강제 검사에 추가한 뒤
재실행하여 256개 평가 필드가 모두 일치했습니다.

리팩터링 후 회귀 테스트 결과는 `28 passed`입니다.
