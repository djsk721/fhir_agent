# 파일럿 독립 매핑 참조

이 파일들은 연구 코드의 `evaluate --reference-profiles`에서만 사용하는 부분 참조입니다.
등록된 추출 명세의 컬럼 의미, 주키·외래키와 FHIR R4의 대상 요소를 근거로
평가 규칙을 별도로 정의했습니다. 비교할 초기·보정 Profile이나 독립 참조 FHIR의
변환 결과를 정답 규칙으로 복사하지 않았습니다. 원천 행, 환자 ID, 실험 모델 출력은
이 디렉터리에 포함하지 않습니다.

| 파일 | 항목 수 | 핵심 계약 |
| --- | ---: | --- |
| `mimic_iv-pilot.json` | 4 Profile, 15 매핑 | `subject_id` 및 `hadm_id`는 업무키 기반 참조로 사용합니다. `valuenum`이 있으면 Quantity를, 없으면 문자 원문을 사용합니다. 단위는 수치 분기에서만 보존합니다. |
| `synthea-pilot.json` | 4 Profile, 18 매핑 | `id`를 환자·입원 리소스의 ID로 사용합니다. `patient` 및 `encounter`는 대응 업무키를 참조합니다. Observation의 `type`이 `numeric` 또는 `text`이면 각각 Quantity 또는 `valueString`을 생성합니다. |

두 참조의 공통 대상은 Patient의 성별·사망일, Encounter의 환자 참조·기간,
Condition의 진단 코드·환자 및 입원 참조, Observation의 코드·값·단위·시간과 환자
및 입원 참조입니다. Synthea는 출생일과 Condition 시작·종료일을 추가합니다.
모호한 타임존을 임의로 지정하지 않으며, 입원 상태·분류, Observation 상태,
로컬 코드의 표준 용어 체계 매핑 등 원천 명세만으로 확정하기 어려운 규칙은
이 파일에 포함하지 않습니다. 이 범위는 Bundle을 완성하는 전체 변환 명세가
아닙니다. FHIR 적합성은 별도의 전체 출력과 실제 Validator로 평가합니다.

MIMIC 평가 옵션에 `--reference-profiles experiments/references/mimic_iv-pilot.json`을,
Synthea에는 `--reference-profiles experiments/references/synthea-pilot.json`을
추가합니다. 고정 코호트에서는 `cohort-lock`,
`cohort-verify`, `evaluate`에 해당 데이터셋의 같은 파일을 전달합니다. 이 파일은
`generate-profile`, RAG 인덱스, 보정 피드백의 입력으로 사용하지 않습니다.

현재 점수는 예측과 참조의 규칙 표현을 비교합니다. 부분 참조에 없는 예측 규칙도
정밀도 분모에 들어가므로 낮은 정밀도가 모두 의미 오류를 뜻하지 않습니다.
연산 설정이 다르더라도 임상 결과가 동등할 수 있습니다. 파일럿에서는 비교기와
분모를 확인하는 기술 지표로 해석합니다. 본실험 전에는 대상 필드와 허용할 동등
표현을 고정하고 별도 검토자가 평가한 정답 및 예를 준비합니다. 값 충실도와
누락은 독립 참조 FHIR 및 원천 기대 수에 대한 점수로 별도 보고합니다.
