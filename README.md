# 임상 DB → LLM → FHIR

이 프로젝트는 별도 PostgreSQL 서버의 임상 데이터를 LLM으로 조회하고, 조회 결과를
FHIR R4 리소스로 변환한 뒤 검증합니다. 기본 제공 source profile은 MIMIC-IV 2.2이며,
병원별 DB 구조와 매핑은 Pipeline 코드가 아니라 프로필 파일로 교체할 수 있습니다.

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

## Source profile: 타 병원 DB 적용

MIMIC 전용 정책은 [MIMIC source profile](./profiles/mimic_iv/profile.json)에 모았습니다.

- `profile.json`: DB schema, 테이블 역할, 매핑·SQL 템플릿 경로, Bundle namespace, cohort 추출 정책
- `queries.json`: 리소스별 LLM SQL 질문 템플릿
- `mapping JSON`: 논리 필드→물리 컬럼, 식별자, ID, 성별, Encounter, ICD/UCUM 정책

Pipeline, LLM 생성, Bundle 조립, 검증 코드는 `patient`, `encounter`, `condition`,
`procedure`, `observation`이라는 논리 역할만 사용합니다. 따라서 타 병원 적용 시
[hospital_sample 예시](./profiles/hospital_sample/profile.json)를 복사해 세 파일의
물리 schema·테이블·컬럼과 용어 정책을 바꾸고 다음 한 줄만 지정하면 됩니다.

```dotenv
SOURCE_PROFILE_PATH=./profiles/my_hospital/profile.json
```

프로필을 바꾸면 VectorDB 문서 내용도 달라지므로 반드시 `build-vectors`를 다시 실행합니다.
`SQL_ALLOWED_SCHEMAS`와 `SQL_ALLOWED_TABLES`도 새 DB의 최소 허용 범위로 맞춰야 합니다.
현재 공통 Pipeline이 지원하는 FHIR 리소스는 5종이며, 새 리소스를 추가하려면 공통
`ResourceAdapter`와 해당 프로필의 query/mapping 계약을 함께 추가해야 합니다.

## 현재 구현 내용

### 핵심 실행 경로

`LlmSqlToFhirPipeline.run()`은 다음 순서로 동작합니다.

1. `.env`, source profile과 mapping JSON을 읽고 설정값·리소스 의존성을 검사합니다.
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

실제 테이블명과 컬럼명은 source profile 및 매핑 JSON에서 변경할 수 있습니다. ID는 코드에서 문자열을
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
SOURCE_PROFILE_PATH=./profiles/mimic_iv/profile.json
SOURCE_DB_URL=postgresql+psycopg2://사용자:비밀번호@DB서버:5432/mimiciv-2.2
SOURCE_DATA_PATH=/home/mdhi/physionet.org/files/mimic-iv-fhir-demo
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=gemma4:e4b
OLLAMA_EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIMENSION=768

FHIR_SCHEMA_PATH=./db/fhir.schema.json
FHIR_DEFAULT_TIMEZONE=Z
```

주요 실행 정책:

| 설정 | 의미 | 예시 |
|---|---|---:|
| `SOURCE_PATIENT_ID` | 명령행에서 생략할 기본 환자 ID | 빈 값 |
| `FHIR_OUTPUT_RESOURCES` | 생성할 리소스 | 5종 전체 |
| `SOURCE_MAX_ENCOUNTERS` | 환자당 최근 입원 수 | `1` |
| `SOURCE_MAX_CONDITIONS_PER_ENCOUNTER` | 입원당 진단 수 | `10` |
| `SOURCE_MAX_PROCEDURES_PER_ENCOUNTER` | 입원당 처치 수 | `10` |
| `SOURCE_MAX_OBSERVATIONS_PER_ENCOUNTER` | 입원당 검사 수 | `10` |
| `SQL_MAX_ROWS` | 안전 Gateway의 최종 행 제한 | `100` |
| `LLM_SQL_MAX_ATTEMPTS` | SQL 자동 교정 포함 최대 호출 | `2` |
| `LLM_FHIR_MAX_ATTEMPTS` | FHIR 배치 자동 교정 포함 최대 호출 | `2` |
| `LLM_FHIR_BATCH_SIZE` | FHIR LLM 호출당 소스 행 수 | `2` |
| `LLM_FHIR_CONTEXT_SEARCH_K` | FHIR Vector 문맥 수 | `20` |
| `FHIR_VALIDATOR_JAR` | HL7 Validator CLI jar | 선택 사항 |

MIMIC의 소스 컬럼, 성별, Encounter class/status, ID 생성, ICD와 UCUM 규칙은
[mimic_mapping_config.json](./db/mimic_mapping_config.json)에서 변경합니다.
기존 `MIMIC_*` 환경 변수는 기본 MIMIC profile에서 하위 호환됩니다.

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
| `public.fhir_mapping_vectors` | FHIR R4 JSON Schema와 선택한 source→FHIR 매핑 정책 |
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
python main.py convert --subject-id 10000032 --encounter-id 25742920
```

`--encounter-id`는 선택 사항입니다. 생략하면 설정된 개수만큼 최근 입원을 조회합니다.
기존 `--hadm-id`도 호환 별칭으로 동작합니다.

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

`--limit`과 `--all`의 환자 목록은 DB 전체 환자가 아니라 선택한 source profile의
`cohort.patient_file_patterns`에 해당하는 `SOURCE_DATA_PATH` 아래 Patient NDJSON에서 읽습니다.
기본 MIMIC profile은 최신 `fhir/MimicPatient.ndjson.gz`를 선택하고, profile에 지정된
`identifier_system`의 `identifier[].value`를 DB 환자 키로 사용합니다.

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

## Source profile 리팩토링 검증

2026-07-24 기본 MIMIC profile로 VectorDB를 다시 구축하고 demo 환자 `10007928`을
DB→LLM SQL→LLM FHIR→검증까지 실행했습니다.

- VectorDB: FHIR 문서 11,503건, SQL 문서 10건, 두 테이블 모두 `vector(768)`·HNSW 호환
- 조회 행: Patient 1, Encounter 1, Condition 10, Procedure 1, Observation 10
- 생성 결과: 23개 리소스, 누락·과생성 0건
- FHIR Validator: 통과, fatal/error 0건, warning 56건
- Source Field Accuracy, Resource Count Alignment, Condition/Procedure/Observation F1,
  Reference Resolution, Coding Accuracy: 모두 `1.0`
- 자동 교정: 13개 배치, first-pass `53.85%`, repair 6회, Repair Success Rate `100%`
- 저장된 `fhir_output/bundle.json` 독립 재검증: 통과

이 실행은 source profile 분리 후에도 기본 MIMIC 동작이 유지되는지 확인한 smoke test입니다.
타 병원 예시 프로필은 DB 접속 정보가 없는 구조 예시이므로 물리 SQL 질문 생성까지 회귀
테스트하며, 실제 병원 DB end-to-end 결과를 의미하지 않습니다.

## 100명 전체 cohort 실행 결과

2026-07-24 `MimicPatient.ndjson.gz`의 전체 100명을 대상으로 실행한
`fhir_export/manifest.json` 결과입니다.

### End-to-end 결과

| 구분 | 환자 수 | 전체 100명 기준 |
|---|---:|---:|
| 생성 완료 | 46 | 46% |
| FHIR Validator 통과 | 42 | 42% |
| FHIR Validator 실패 | 4 | 4% |
| 생성 단계 실패 | 54 | 54% |
| FHIR와 Task 품질 모두 통과 | 33 | 33% |

생성된 46개 Bundle만 분모로 사용하면 FHIR Validation Pass Rate는
`42 / 46 = 91.30%`입니다. 그러나 전체 cohort의 엄격한 end-to-end 성공률은
FHIR 검증과 DB 원본 기반 Task 품질을 모두 통과한 `33 / 100 = 33%`입니다.

Manifest의 `overall_task_pass_rate=0.7609`는 생성 완료 Bundle 중
`task_quality_pass=true`인 `35 / 46`을 나타냅니다. FHIR 검증까지 포함한
end-to-end 통과율과 구분해서 해석해야 합니다.

### 생성 리소스와 조건부 품질

생성 완료된 46명에서 총 1,128개 리소스가 생성됐으며, 환자당 평균은
약 24.5개입니다.

| 리소스 | 생성 수 | 생성 완료 환자당 평균 |
|---|---:|---:|
| Patient | 46 | 1.00 |
| Encounter | 46 | 1.00 |
| Condition | 415 | 9.02 |
| Procedure | 161 | 3.50 |
| Observation | 460 | 10.00 |

| 평가 지표 | 결과 | 설명 |
|---|---:|---|
| Source Field Accuracy | 99.77% | SQL 조회 원천 필드와 생성된 FHIR 필드가 정확히 일치한 비율 |
| Clinical Resource Macro F1 | 100% | Condition, Procedure, Observation의 리소스별 F1을 동일 가중치로 평균한 값 |
| Resource Count Alignment | 100% | SQL 소스 행 수와 생성 FHIR 리소스 수가 일치한 비율 |
| Reference Accuracy | 100% | Patient와 Encounter 참조가 원천 행의 대상과 정확히 일치한 비율 |
| Coding Accuracy | 100% | ICD·검사 코드의 system/code와 소스 display, UCUM 코드가 기대값과 일치한 비율 |
| JSON Schema Pass Rate | 100% | 생성 리소스가 공식 FHIR R4 JSON Schema를 통과한 비율 |
| Observation Value Accuracy | 98.47% | Observation의 valueQuantity 또는 valueString이 원천 검사값과 일치한 비율 |
| Observation Unit Accuracy | 98.90% | Observation 값과 참고범위의 unit 및 UCUM code가 기대값과 일치한 비율 |
| Observation Time Accuracy | 99.13% | Observation.effectiveDateTime이 원천 charttime과 일치한 비율 |
| Encounter Field Accuracy | 98.26% | Encounter의 ID, 상태, class, 기간, 식별자와 Patient 참조가 원천·매핑 규칙과 일치한 비율 |

이 결과는 생성에 성공한 46명과 LLM SQL이 반환한 소스 행을 기준으로 합니다.
실패한 54명과 SQL이 조회하지 못한 DB 행은 Accuracy/F1 분모에 포함되지 않으므로,
위 수치를 전체 cohort의 end-to-end 정확도로 해석하면 안 됩니다.

### 실패 원인

54건의 생성 실패는 다음 두 원인에 집중됐습니다.

| 실패 원인 | 환자 수 | 전체 실패 중 비율 |
|---|---:|---:|
| Procedure 소스 행 0건을 오류로 처리 | 22 | 40.7% |
| Observation이 2회 교정 후에도 실패 | 32 | 59.3% |

Procedure가 없는 환자는 정상적인 임상 사례일 수 있으므로, 선택 임상 리소스의
0건 조회를 정상 결과로 허용하지 않는 정책이 전체 완주율을 크게 낮췄습니다.

Observation 실패는 주로 다음 값의 null/빈 문자열 처리와 UCUM 변환에서 발생했습니다.

- `valueQuantity.unit` 누락 또는 빈 값: 24건
- `valueQuantity.code` 누락: 22건
- `referenceRange`의 value/unit/code 오류: 16건
- 기대 UCUM 코드 불일치: 15건
- 숫자 `valueQuantity.value`가 null: 2건

위 항목은 한 실패에 여러 오류가 함께 나타날 수 있으므로 중복 집계입니다.

### HL7 FHIR Validator 결과

| Severity | 누적 개수 |
|---|---:|
| fatal | 0 |
| error | 21 |
| warning | 2,922 |
| information | 17 |

Validator error가 발생한 환자는 `10004720`, `10026354`, `10018423`,
`10015931`입니다. 주요 error는 UCUM 코드 `mm Hg`, 비표준 단위 `units`,
일부 ICD-10 Procedure 코드 검증 실패입니다. `mm Hg`의 UCUM 코드는
`mm[Hg]`로 정규화해야 합니다.

주요 warning은 MIMIC의 시간 이동으로 인한 2100년 이후 날짜, Validator가 알지
못하는 `urn:mimic:itemid` CodeSystem, narrative와 Observation performer 누락,
표준 코드 없이 text만 있는 interpretation입니다. 누적 warning은 생성 Bundle당
평균 63.5건, 리소스당 약 2.59건입니다.

### LLM SQL과 자동 교정

생성 완료 46명에 기록된 LLM 호출은 총 939회이며, 환자당 평균 20.41회,
중앙값 20회입니다. 실패한 환자의 호출 횟수는 manifest에 남지 않으므로 실제
전체 호출량은 이보다 큽니다.

| 항목 | 결과 |
|---|---:|
| FHIR 생성 배치 | 624 |
| 배치 First-pass Validity Rate | 93.75% |
| 기록된 repair 횟수 | 39 |
| 기록된 Repair Success Rate | 100% |
| SQL 질의 | 230 |
| SQL First-pass Success Rate | 80% |
| SQL DB 실행 실패 후 교정 | 46 |

리소스별 FHIR 배치 first-pass 통과율은 Patient 82.61%, Encounter 100%,
Condition 100%, Procedure 73.91%, Observation 96.96%입니다.

`Repair Success Rate=100%`는 생성 완료 환자에 포함된 repair만 집계한 값입니다.
Observation 교정에 최종 실패한 32명의 telemetry는 집계되지 않으므로 전체 cohort의
실제 repair 성공률로 사용할 수 없습니다. 또한 생성 완료 환자마다 SQL DB 실행
교정이 정확히 한 번 발생했지만, manifest에는 실패한 리소스 종류와 SQL이 없어
체계적인 원인을 추가로 추적해야 합니다.

### 결과 해석과 개선 우선순위

현재 결과는 “생성에 성공했을 때의 변환 정확도는 높지만, 전체 환자를 끝까지
처리하는 운영 완주율은 낮다”로 요약할 수 있습니다. RAG는 활성화됐지만 비교
실행이 없어 `rag_ablation_improvement`는 `null`이며, 현재 결과만으로 RAG의
개선 효과를 결론 내릴 수 없습니다.

우선순위는 다음과 같습니다.

1. Procedure 등 선택 임상 리소스의 0건 조회를 정상 결과로 허용
2. Observation null 필드를 LLM 호출 전에 결정적으로 생략
3. `mm Hg → mm[Hg]` 등 UCUM 정규화 규칙 보강
4. 실패 환자도 SQL/FHIR 호출 및 repair telemetry를 manifest에 보존
5. `overall_task_pass_rate`와 strict end-to-end pass rate를 분리
6. 동일 cohort의 LLM-only와 LLM+RAG paired ablation 실행

## 코드 구조

```text
main.py                                  CLI 진입점
fhir_agent_pipeline/cli.py               명령 파싱과 결과 저장
fhir_agent_pipeline/config.py            .env 설정과 범위 검증
fhir_agent_pipeline/source_profile.py    병원별 DB·query·cohort profile 로더
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
profiles/mimic_iv/                       기본 MIMIC source/query profile
profiles/hospital_sample/                타 병원 물리 schema·컬럼 교체 예시
tests/                                   DB/Ollama 없이 실행하는 회귀 테스트
```
