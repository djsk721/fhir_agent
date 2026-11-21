# SQL Planning을 제외한 Hybrid FHIR 연구 구조

논문의 주 실행 경로는 `python main.py research ...` 또는
`python -m research.fhir_profile ...`입니다. 원천 조회는 검토된 추출 명세로
수행하며 LLM이 SQL·조인·조회 범위를 결정하지 않습니다. 기존
`fhir_agent_pipeline`의 LLM SQL 경로는 별도 운영·비교용으로 유지합니다.

## 개발과 평가의 분리

```text
MIMIC development/validation
  → 프롬프트·모델·검색·교정 정책 개발
  → FHIR Schema index 구축
  → policy.lock.json으로 정책 고정
       ├─ MIMIC 준비 환자 → MIMIC Conversion Profiles → MIMIC test
       └─ Synthea calibration → Synthea Conversion Profiles → Synthea test
```

Synthea에서는 동일 프롬프트와 공통 Renderer로 별도 Profile을 생성합니다.
MIMIC Profile을 다른 스키마에 그대로 적용하지 않습니다. 이 평가는 합성 외부
데이터셋을 이용한 교차 스키마 검증이며 실제 타 병원 EMR 검증과 구분합니다.

## Profile 준비 단계

```text
검토된 추출 명세 + 준비 환자
  → CSVRecordExtractor / PredefinedQueryExtractor
  → Source Records + 원천 metadata
  → Mapping Agent ← FHIR Schema Vector 검색
  → 초기 Conversion Profile (P1)
  → Generic Renderer
  → JSON Schema·참조·HL7 검증
  → Mapping Agent의 Profile 수정
  → 동일 원천 재변환·재검증
  → 교정된 Conversion Profile (P2) 고정
```

교정 입력은 준비 환자의 원천 보존 assertion 실패와 표준 검증 오류입니다.
assertion은 생성 Profile과 독립적으로 정책에 고정합니다. 독립 정답 FHIR와
평가 환자는 입력하지 않습니다. 원천 테이블·대상 리소스·identity 변경이나
원천 필드 삭제를 통한 오류 은폐는 거부합니다. 교정 전후 Profile과 이력을
함께 보존합니다. 오류 multiset을 비교하여 신규 차단 오류가 생긴 후보는
거부하고, 해결·신규 오류 수와 거부된 후보 이력을 기록합니다.

## 평가 단계

세 비교군은 고정된 동일 평가 Source Records를 사용합니다.

| 군 | FHIR 생성 방법 | 평가 중 Profile 수정 |
| --- | --- | --- |
| B1 | Local LLM이 FHIR 직접 생성 | 없음 |
| P1 | 초기 Profile을 결정적 Renderer로 실행 | 없음 |
| P2 | 준비 단계에서 교정한 Profile을 실행 | 없음 |

검증 결과는 표준 적합성, 행 수 대비 완전성, 참조 무결성, 독립 참조값 정확도를
나누어 기록합니다. 독립 정답이 없으면 충실도를 `not_evaluated`로 표시하며
100%로 처리하지 않습니다. 같은 Profile을 다시 실행한 결과의 일치성은
`profile_execution_consistency`이고 매핑 정답 정확도가 아닙니다.

## 구성요소와 산출물

| 구성요소 | 역할 | 주요 위치 |
| --- | --- | --- |
| 환자 분할 | 재현 가능한 환자 단위 분할·중복 검사 | `research/fhir_profile/splits.py` |
| Data Extractor | 사전 정의한 CSV·DB 추출 | `research/fhir_profile/data/` |
| Mapping Agent | 원천 metadata와 FHIR 문맥으로 Profile 생성·교정 | `research/fhir_profile/agents/mapping.py` |
| FHIR Retriever | FHIR Schema만 검색 | `research/fhir_profile/runtime.py` |
| Generic Renderer | 허용된 선언적 연산 실행 | `research/fhir_profile/renderer/` |
| 검증·비교기 | Schema·HL7·독립 정답·참조 검사 | `research/fhir_profile/validation/`, `experiment.py` |
| 정책 잠금 | 설정·코드·모델 digest·스키마·index·Validator 식별 | `research/fhir_profile/artifacts.py` |

연구용 RAG는 FHIR 정의를 담은 로컬 Vector index JSON을 사용합니다. 기존
운영 경로의 PostgreSQL `source_sql_schema_vectors`는 사용하지 않습니다.
원천 스키마 정보는 추출 metadata로 Mapping Agent에 전달합니다.

데이터셋별 테이블·컬럼 정보는 `data/specs/`에, 변환 규칙은 Conversion Profile에
둡니다. 따라서 병원 데이터에 의존하는 정보가 없는 완전 무설정 시스템이라는
주장은 하지 않습니다. 공통 Renderer 재사용과 Profile 자동 생성 능력이
연구 대상입니다.

실행 순서·평가 범위와 재현 조건은 [연구 프로토콜](./research_protocol.md),
Profile 형식은 [Conversion Profile 규칙](./conversion_profile.md)을 참고합니다.
