# Conversion Profile v1

Conversion Profile은 원천 테이블의 행을 FHIR R4 리소스로 바꾸는 재사용 명세입니다.
SQL 조회 명세, cohort 선택 설정, 개별 환자의 FHIR 결과와 구분합니다.
연구 범위는 Patient, Encounter, Condition, Observation입니다.

필수 항목:

- `profile_id`, `profile_version`
- `target_resource`: Patient, Encounter, Condition, Observation
- `fhir_version`: `4.0.1` 또는 `R4`
- `source.dataset`, `source.table`
- `identity.source_fields`
- `mappings`

각 mapping은 `id`, `target_path`, `operation`을 가집니다. 원천 값이 없는 선택
필드는 기본 `null_policy: omit`으로 요소 자체를 생략합니다. 필수 원천값 누락을
차단하려면 `null_policy: error`를 사용합니다.

허용 연산은 `string`, `integer`, `decimal`, `boolean`, `date`, `datetime`,
`constant`, `lookup`, `reference`, `coding`, `codeable_concept`, `identifier`,
`copy`입니다. `lookup`은 Profile 안의 명시적 표만 실행하며 외부 코드 검색이나
임의 프로그램 실행을 수행하지 않습니다.

`target_path`는 FHIR Schema의 중첩 경로와 배열 index까지 검사합니다. 원천에
없는 필드 참조, 동일 경로의 충돌, 상호 배타성이 입증되지 않은 `value[x]` 충돌은
사전 거부합니다. `id`와 `resourceType`은 Renderer가 관리합니다.

## 값 유형과 날짜

Synthea CSV의 숫자·문자 Observation을 하나의 값 타입으로 강제하지 않습니다.
명시적 `when` 조건으로 다른 FHIR choice를 선택할 수 있습니다.

```json
{
  "id": "numeric-result",
  "target_path": "valueQuantity.value",
  "operation": "decimal",
  "source_field": "value",
  "when": {"field": "type", "equals": "numeric"}
}
```

문자 결과에는 같은 조건 필드를 사용한 상호 배타적인 조건과 `valueString`을
지정합니다. `when`에는 `equals`, `in`, `present`, `all`, `any`, `not`만 허용합니다.
조건 비교는 타입을 보존하므로 CSV 문자열 `"1"`과 DB 정수 `1`은 다릅니다.

숫자 변환은 유한한 값만 허용하며 소수를 integer로 절삭하지 않습니다. 날짜의
정밀도와 timezone offset은 보존합니다. timezone이 없는 timestamp에는 승인된
명시적 timezone이 필요하며 UTC를 임의로 붙이지 않습니다.

## 식별자와 참조

`identity.source_fields`에 원천 이벤트를 구분할 키를 지정합니다. 단일 키는
가능하면 그대로 사용하고, FHIR ID 문자·길이 제한을 벗어나거나 복합키 연결이
모호하면 구조화된 키의 SHA256을 사용합니다. 같은 배치에서 중복 ID가 발생하면
오류로 기록합니다.

`reference` 연산은 대상 리소스와 같은 identity 규칙을 사용해야 합니다.
복합 ID를 사용하면 reference mapping에도 동일한 `identity`를 선언합니다.
생성 결과에 참조 대상이 실제 존재하는지와 환자 소속이 일치하는지는 별도로
검증합니다. `Patient/` 또는 `/` 포함 여부만으로 정상 참조로 판정하지 않습니다.

CSV 추출기의 `_source_record_id`는 파일과 원천 행의 계보를 기록하는 내부
필드입니다. FHIR payload에 복사하지 않으며, 임상 이벤트 identity는 가능한 한
원천의 안정적인 업무키로 정의합니다.

예시 파일: `research/fhir_profile/profiles/examples/observation.json`

금지 항목:

- `sql`, `query`, `statement`
- 허용 목록 밖의 operation
- 대상 리소스의 FHIR Schema에 없는 경로
- metadata에 없는 원천 컬럼
- 중복 mapping ID와 충돌하는 target path

## Profile 생성·교정·고정

Mapping Agent는 준비 환자의 metadata·샘플·FHIR RAG로 초기 Profile을 만듭니다.
Validator 오류가 있으면 동일 Agent가 Profile을 수정합니다. 원천 행은 변경하지
않으며 SQL 생성도 수행하지 않습니다. 기존 보조 함수의 status·timezone·단위
규칙은 기본 활성화하지 않고 명시적으로 승인한 정책이 있을 때만 사용합니다.

교정 시 source·target resource·identity 변경과 원천 필드 삭제를 거부합니다.
차단 오류가 감소하고 신규 차단 오류가 없는 수정만 채택하고 변경 이력을
기록합니다. 정책에 고정한 `source_checks`는 별도의 원천 보존 기준으로
실행하며, 검사 실패 target의 잘못된 컬럼 교체는 재검증으로 판단합니다. 이 조건이 의미
정확성을 증명하지는 않으므로 독립 정답 평가를 따로 수행합니다.

독립 정답 FHIR나 참조 매핑은 Profile 생성·교정 입력과 채택 기준에 넣지
않습니다. `profile_execution_consistency`는 실행 일치성이고, 독립 정답 대비
충실도와 구분합니다. 정답이 없으면 충실도를 미평가로 남깁니다.

Profile artifact는 초기·교정 Profile, 준비 cohort hash·환자 목록, 정책 hash,
실제 LLM 호출 이력과 시간을 보존합니다. 평가 환자로 Profile을 만들거나
평가 중 Profile을 수정하는 실행은 거부합니다. Synthea에는 고정된 프롬프트로
별도 Profile을 만들며 MIMIC Profile을 재사용하지 않습니다.
