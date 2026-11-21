# Conversion Profile 기반 FHIR 변환 연구

논문용 주 실행 경로는 **SQL Planning 없는 Conversion Profile 기반 Hybrid 변환**입니다.
MIMIC에서 프롬프트를 개발·고정하고 Synthea에서 합성 외부 평가를 수행하는
[연구용 실행 안내](#연구용-hybrid-profile-파이프라인)를 참고하세요.
아래의 기존 `export-validate` 명령은 별도로 유지하는 LLM SQL 경로입니다.

연구 경로는 검토된 명세로 원천 행을 추출하고, Local LLM이 생성한 재사용
Conversion Profile을 공통 Renderer로 실행합니다. 실제 HL7 Validator와 독립
참조로 검증하고 환자 단위로 B1/P1/P2 및 RAG 유무를 비교합니다.

```text
고정 Extractor → 원천 행·metadata → Mapping Agent + FHIR RAG
→ 초기 Profile → 원천 보존·표준 검증 → Profile 교정
→ 고정 초기/교정 Profile → 별도 환자 평가 → 대응 통계·비용 보고
```

[연구 구현 안내](./docs/research_implementation.md)와
[검증 기록](./docs/research_verification.md)에 보정 규칙, 평가 지표와 실행 결과를
정리했습니다. 단계별 실행은 [test.ipynb](./test.ipynb)를 사용합니다.

아래 기존 경로는 PostgreSQL metadata를 검색해 LLM이 SQL을 생성하고,
조회 결과를 FHIR R4 리소스로 변환합니다.

핵심 흐름은 다음과 같습니다.

```text
PostgreSQL metadata
  → DB Schema Vector DB
  → LLM SQL 생성·실행·Self-Correction
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
- 코드체계 URI, display, UCUM 단위처럼 DB에 없는 값은 추측하지 않고
  `NULL`로 둡니다.
- Source profile은 DB 매핑이 아닙니다. 공통 질문 계약, Bundle namespace,
  선택적인 Patient NDJSON cohort 읽기 방식만 정의합니다.

MIMIC demo를 사용할 때만
[`profiles/mimic_iv/profile.json`](./profiles/mimic_iv/profile.json)을 선택합니다.
다른 DB는 이 파일을 복사해 테이블 매핑을 작성할 필요가 없습니다. 직접 환자
key로 실행할 경우 cohort provider가 `none`인 profile도 사용할 수 있습니다.

## 특정 병원 DB 정보의 범위

`python main.py`로 실행하는 기존 파이프라인에는 MIMIC 스키마·테이블·컬럼명을
고정하지 않습니다. 예를 들어 `mimiciv_hosp`, `admissions`, `labevents` 같은
이름은 SQL 생성 규칙이나 FHIR 변환 로직에 들어 있지 않습니다. 논문 실험용
`research/fhir_profile/data/specs/`만 데이터셋별 검증 조회 명세를 따로 가집니다.

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

`.env`는 `python main.py`의 기존 LLM SQL 파이프라인이 읽습니다. 연구용
Profile 파이프라인은 이 파일의 조회 상한을 사용하지 않고, 명령에 전달한
Profile과 Source Records를 사용합니다.

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

현재 로컬 `.env`의 운영 상태는 다음과 같습니다.

- source profile: `profiles/mimic_iv/profile.json`
- cohort 파일 위치: MIMIC-IV FHIR demo Patient NDJSON
- `SOURCE_PATIENT_ID`: 비움. 단일 환자는 `--patient-key`로 지정
- 생성 리소스: Patient, Encounter, Condition, Procedure, Observation
- `FHIR_EXPORT_DIR`: `./fhir_export_demo_generic_10`
- Ollama model: `gemma4:e4b`
- Vector 테이블: `source_sql_schema_vectors`, `fhir_schema_vectors`
- 외부 Validator: `./validator_cli.jar`

조회 상한은 코드가 허용하는 최댓값으로 맞춰져 있습니다.

| 설정 | 현재 값 | 허용 범위 | 실제 적용 단위 |
| --- | ---: | --- | --- |
| `SQL_MAX_ROWS` | 10000 | 1~10000 | SQL 실행 1회 |
| `SOURCE_MAX_ENCOUNTERS` | 100 | 1~100 | 환자 1명 |
| `SOURCE_MAX_CONDITIONS_PER_ENCOUNTER` | 10000 | 1~10000 | 선택된 Encounter 전체에 합산 후 `SQL_MAX_ROWS`로 다시 제한 |
| `SOURCE_MAX_PROCEDURES_PER_ENCOUNTER` | 10000 | 1~10000 | 위와 같음 |
| `SOURCE_MAX_OBSERVATIONS_PER_ENCOUNTER` | 10000 | 1~10000 | 위와 같음 |

따라서 임상 리소스의 실제 반환 상한은 입원마다 10,000건이 아니라, 환자 1명의
리소스별 SQL당 최대 10,000건입니다. 설정명이 `PER_ENCOUNTER`여도 현재 구현은
입원별 row 분배를 보장하지 않습니다.

DB URL, source profile, data path는 환경별 접속 설정이며 테이블·컬럼 매핑이
아닙니다. `FHIR_VALIDATOR_TERMINOLOGY_SERVER`는 읽지 않으므로 사용하지 않습니다.

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
python main.py export-validate --limit 10
```

Source profile의 Patient NDJSON cohort 전체:

```bash
python main.py export-validate --all
```

`--all`은 DB의 모든 환자가 아니라 선택한 source profile의 cohort 파일에
들어 있는 모든 환자를 뜻합니다. 각 key로 DB를 새로 조회하고 LLM SQL과 FHIR
생성을 수행합니다. 중단 후 이어서 실행하려면 같은 출력 경로에 `--resume`을
붙입니다. `--resume`은 Validator와 저장된 task 평가가 모두 통과한 환자만
재사용하며 invalid, failed, task 평가 실패 환자는 다시 생성합니다.

```bash
python main.py export-validate --all --resume
```

## 4. SQL 안전성과 Self-Correction

생성 SQL에는 다음 제약이 적용됩니다.

- `SELECT`와 `WITH`만 허용
- DB에서 실제 발견한 schema-qualified relation만 허용
- DDL, DML, row lock, 위험 함수, SQL 주석 차단
- 읽기 전용 transaction과 statement timeout 적용
- 최종 row limit 강제
- 필수 generic alias, 환자/encounter 범위, key 고유성, code 값 검증

실패하면 이전 SQL, DB 오류, 해당 relation의 정확한 metadata를 다시 LLM에
제공합니다. CTE alias와 `alias.column`은 SQL 구조 및 DB metadata로 증명되는
경우에만 보정합니다. 선택 필드가 실제 컬럼으로 입증되지 않으면 임의의 병원별
대체값을 넣지 않고 `NULL`로 낮춥니다.

LLM SQL이 반환하는 공통 alias는
[`source_contract.py`](./fhir_agent_pipeline/source_contract.py)에 정의되어
있습니다. 이 alias는 DB 물리 컬럼 매핑이 아니라 DB와 FHIR 단계 사이의
데이터 계약입니다.

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

## 실제 검증 결과

> 아래 수치는 과거 운영 파이프라인의 예비 결과 기록입니다. 신규 연구 경로의
> Direct/Hybrid 비교나 Synthea 외부검증 결과가 아니며, 100% 수치를 논문 성능
> 근거로 사용하기 전에 보존된 원본 산출물과 평가 기준을 다시 확인해야 합니다.

2026-07-26, 현재 `.env`와 `gemma4:e4b`로 demo Patient NDJSON 앞 10명을
실행했습니다. Demo 파일은 대상 key를 정하는 데만 사용했고 임상 내용은 각
key로 DB를 조회해 새로 생성했습니다.

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

결과 파일:

- [10명 생성 manifest](./fhir_export_demo_generic_10/manifest.json)
- [독립 재검증 집계](./fhir_export_demo_generic_10/revalidation.json)
- 환자별 `patient-<key>.bundle.json`, `.validation.json`, `.trace.json`

## 결과 디렉터리 관리

현재 프로젝트 루트에는 최종 검증 결과인
`fhir_export_demo_generic_10/`만 보존합니다. 이전 실패·중간·단일 환자 실험
디렉터리는 2026-07-26에 시스템 휴지통으로 이동했습니다.

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

## 연구용 Hybrid Profile 파이프라인

현재 논문의 주 연구 경로는 `research/fhir_profile`입니다. SQL Planning을
제외하고, **MIMIC에서 프롬프트·검색·교정 정책을 개발한 뒤 Synthea로 합성
외부검증**을 수행합니다. 위에서 설명한 기존 LLM SQL 운영 경로와 실행 명령은
계속 사용할 수 있으며 연구 명령은 `python main.py research ...`로 구분합니다.

```text
검토된 추출 명세 → 원천 행·metadata
고정된 Mapping Agent + FHIR Schema RAG → Conversion Profile
준비 환자에서 Profile 교정 → Profile 고정
별도 평가 환자 → 공통 Renderer → FHIR 생성·검증·독립 참조 비교
```

LLM은 환자마다 FHIR를 작성하는 대신 재사용할 변환 명세를 만듭니다. 예를 들어
검사값을 `Observation.valueQuantity.value`로, 검사시간을 `effectiveDateTime`으로
보내는 규칙을 Profile에 기록하고 공통 코드가 전체 원천 행에 적용합니다.
연구 리소스는 Patient, Encounter, Condition, Observation 4종입니다.

| 단계 | 데이터 | 하는 일 |
| --- | --- | --- |
| 방법 개발 | MIMIC development/validation | 프롬프트·모델·RAG·교정 정책 결정 |
| 내부 평가 | MIMIC test | 준비 단계에서 고정한 MIMIC Profile 평가 |
| 외부 Profile 준비 | Synthea calibration | 동일한 프롬프트로 Synthea용 Profile 생성·자동 교정 |
| 합성 외부평가 | Synthea test | 고정한 Synthea Profile 평가 |

MIMIC와 Synthea는 서로 다른 Profile을 사용합니다. 동일한 모델·프롬프트·검색·
교정 정책과 Renderer를 유지하여 다른 원천 스키마에서도 방법이 동작하는지
평가합니다. Synthea 평가 후 정책을 바꿨다면 별도 개발 실험으로 기록합니다.
실제 타 병원 EMR에서의 성능까지 입증한 것으로 해석하지 않습니다.

비교군은 B1(LLM 직접 생성), P1(초기 Profile), P2(준비 환자에서 교정한 Profile)입니다.
모든 군이 같은 평가 원천 행을 사용하며, 평가 환자로 Profile을 생성하거나
정답 FHIR를 보고 교정하는 실행은 거부합니다. 독립 정답이 없으면 충실도를
`not_evaluated`로 남기고, Profile 실행 일치성을 매핑 정확도와 구분합니다.

Synthea 입력은 CSV 또는 DB로 적재한 원천 데이터입니다. 같은 생성 실행의
FHIR R4는 평가용 참조로만 사용합니다. 버전·seed·기준일·모듈 설정과 UUID·
날짜·값 유형을 보존합니다. 독립 참조 ID는 `evaluation.reference_by_dataset`의
identifier 규칙으로 정렬하고 임상 필드와 이벤트 범위는 평가 전에 고정합니다.

연구용 명령:

```bash
python main.py research --help
python -m research.fhir_profile --help

# FHIR 지식 index 생성과 최종 정책 잠금
python main.py research build-rag \
  --config experiments/configs/pilot.json --output artifacts/fhir-index.json
python main.py research freeze \
  --config experiments/configs/pilot.json --index artifacts/fhir-index.json \
  --output artifacts/policy.lock.json
```

전체 순서는 `split → prepare → build-rag → freeze → generate-profile → evaluate`입니다.
`split`은 환자 단위 분할, `prepare`는 CSV·DB 추출, `generate-profile`은
Profile 생성·교정, `evaluate`는 별도 test 환자의 B1/P1/P2 비교를 수행합니다.
lock에는 실제 모델 digest와 코드·프롬프트·index·FHIR Schema·Validator 식별자를
기록하므로 평가 도중 구성 변경을 탐지할 수 있습니다.

실제 실행에는 준비된 MIMIC DB 또는 Synthea CSV, 환자 ID 목록, Ollama 모델과
임베딩 모델, Java·Validator가 필요합니다. 연구 config는
`experiments/configs/pilot.json`이며 운영 `.env`의 조회 상한을 사용하지 않습니다.
연구용 DB 연결은 `prepare --db-url-env SOURCE_DB_URL`로 전달합니다.
기본 연구 추출 명세는 임상 이벤트 LIMIT를 두지 않습니다.

- 단계별 실행 예시·분할·누출 방지·평가: [연구 프로토콜](./docs/research_protocol.md)
- 본실험 연구 질문·비교군·착수 기준: [연구계획서](./docs/research_plan.md)
- 실제 환자 고정 방식·추출과 파일럿 결과: [고정 환자 검증 보고서](./docs/fixed_pilot_report.md)
- 원천 스키마 RAG와 FHIR RAG의 비교 및 실행: [이중 RAG 베이스라인](./docs/dual_rag_baseline.md)
- MIMIC·Synthea 고정 파일럿 매핑 참조 범위: [평가 전용 참조 매핑](./experiments/references/README.md)
- 구성요소와 기존 경로의 차이: [연구 아키텍처](./docs/architecture.md)
- Profile 필드·조건·식별자·교정 규칙: [Conversion Profile](./docs/conversion_profile.md)
- 검토할 추출 명세: `research/fhir_profile/data/specs/`
- Profile 형식 예시: `research/fhir_profile/profiles/examples/observation.json`
- 원천 보존·오류 집계·대응 비교·비용: [연구 구현 안내](./docs/research_implementation.md)
- 테스트와 실제 실행 근거: [검증 기록](./docs/research_verification.md)

새 구조의 기능 테스트와 실제 데이터 실험은 구분합니다. 기능 테스트 통과가
MIMIC 전체 실행 또는 Synthea 외부검증 완료를 의미하지 않습니다. 실제 실험
결과는 `artifacts/results/<run-id>/manifest.json`으로 따로 보존합니다.

## 테스트

```bash
python -m pytest -q
python -m compileall -q fhir_agent_pipeline research main.py
```

연구 파이프라인 회귀 테스트는 `tests/test_fhir_profile_pipeline.py`에 있습니다.

단계별 연구 실행은 [`test.ipynb`](./test.ipynb)를 사용합니다. 환자 분할,
고정 원천 추출, FHIR RAG 구축, 정책 고정, Profile 생성·보정, B1/P1/P2 평가,
RAG 대응 비교 순서로 구성했습니다. 첫 설정 셀에서 데이터·산출물 경로와
실행할 단계의 `RUN_*` 플래그를 지정합니다.
