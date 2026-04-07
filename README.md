# MIMIC-IV Multi-Agent FHIR Pipeline

MIMIC-IV PostgreSQL 데이터에서 환자 1명의 `patients`, `admissions`, `labevents` 로우를 읽고, Ollama 기반 LangChain 멀티 에이전트로 `Patient`, `Encounter`, `Observation` 리소스를 생성한 뒤 하나의 FHIR `Bundle`로 반환하는 예제 파이프라인입니다.

## 구성 요소

- `fhir_agent_pipeline/config.py`
  - `.env`를 읽어 DB/LLM/파이프라인 설정을 로드합니다.
- `fhir_agent_pipeline/db.py`
  - `patients`, `admissions`, `labevents`에서 환자 1명의 데모 데이터를 추출합니다.
- `fhir_agent_pipeline/prompts.py`
  - Manager, Mapping, FHIR Generator, Verifier 프롬프트를 분리해 관리합니다.
- `fhir_agent_pipeline/agents.py`
  - `ChatOllama` 기반 4개 에이전트를 구현합니다.
- `fhir_agent_pipeline/fhir_models.py`
  - `Patient`, `Encounter`, `Observation`, `Bundle`에 대한 경량 검증과 Bundle 생성 유틸리티를 제공합니다.
- `fhir_agent_pipeline/pipeline.py`
  - 리소스별 생성, 검증, 자기수정 루프를 수행하고 최종 Bundle을 조립합니다.
- `main.py`
  - 전체 실행 진입점입니다.

## 에이전트 역할

- `Manager Agent`
  - Verifier 피드백을 받아 재생성 여부와 보정 지시를 결정합니다.
- `Mapping Agent`
  - 원시 MIMIC-IV 데이터를 환자/입원/관측 맥락과 용어 매핑 중심 JSON으로 정규화합니다.
- `FHIR Generator Agent`
  - 요청된 `resourceType`에 맞는 단일 FHIR 리소스를 생성합니다.
- `Verifier Agent`
  - 생성된 리소스를 구조적 관점에서 재검토하고 오류/수정 제안을 JSON으로 반환합니다.

## 지원 리소스

- `Patient`
- `Encounter`
- `Observation`

최종 출력은 위 리소스들을 포함하는 FHIR `Bundle`입니다.

## 동작 흐름

1. `.env`에서 DB와 Ollama 설정을 읽습니다.
2. PostgreSQL에서 환자 1명의 `patients`, `admissions`, `labevents` 데이터를 추출합니다.
3. Mapping Agent가 원시 데이터를 정규화하고 LOINC/SNOMED CT 힌트를 생성합니다.
4. Generator가 `Patient`, `Encounter`, `Observation`을 순서대로 생성합니다.
5. 각 리소스는 Verifier와 경량 Pydantic 검증을 통과해야 합니다.
6. 실패 시 Manager가 보정 지시를 생성하고 재시도합니다.
7. 성공한 리소스를 `Bundle`로 묶어 반환합니다.

## 설치

```bash
pip install -r requirements.txt
```

## 환경 변수

`.env.example`를 참고해 `.env` 파일을 작성하세요.

주요 항목:

- `MIMIC_DB_URL`
- `MIMIC_SCHEMA`
- `MIMIC_PATIENTS_TABLE`
- `MIMIC_ADMISSIONS_TABLE`
- `MIMIC_LABEVENTS_TABLE`
- `PATIENT_SUBJECT_ID`
- `OLLAMA_BASE_URL`
- `OLLAMA_MODEL`
- `FHIR_OUTPUT_RESOURCES`
- `MAX_SELF_CORRECTION_ATTEMPTS`

## 실행

```bash
python main.py
```

성공 시 `Patient`, `Encounter`, `Observation` 리소스를 포함한 FHIR `Bundle` JSON이 stdout으로 출력됩니다.

## 구현 메모

- 비밀값은 코드에 하드코딩하지 않고 `python-dotenv`로 로드합니다.
- FHIR 검증은 전체 스키마가 아닌 경량 검증입니다.
- `labevents` 조회가 실패하면 `Observation` 생성 근거가 부족해질 수 있으므로, 실제 운영 환경에서는 소스 테이블 존재 여부를 먼저 확인하는 것이 좋습니다.
- LLM 출력은 JSON만 받도록 강제하지만, 코드에서 fenced block 제거와 JSON 추출 보정을 한 번 더 수행합니다.
