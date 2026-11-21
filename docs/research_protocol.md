# MIMIC 개발·Synthea 합성 외부검증 프로토콜

본 연구는 Local LLM이 생성한 재사용 Conversion Profile과 결정적 변환을
결합한 Hybrid FHIR 변환을 평가합니다. SQL Planning과 Text-to-SQL은 연구
범위에서 제외하고, 검토된 추출 명세로 동일 원천 데이터를 준비합니다.

연구 질문·분석 계획·실험의 해석 범위는 [연구계획서](./research_plan.md)에 정리합니다.
스키마 전용 초기 생성과 원천 검색 효과의 대응 비교는
[이중 RAG 베이스라인](./dual_rag_baseline.md)의 잠금 및 실행 절차를 사용합니다.
아래는 공통 코호트·평가 계약과 기존 FHIR-only 실행 방법을 함께 설명합니다.

## 연구 질문과 비교군

1. 원천 metadata와 FHIR Schema RAG로 실행 가능하고 올바른 Profile을 만들 수 있는가?
2. Hybrid는 직접 FHIR 생성보다 표준 적합성·충실도·처리 효율에서 어떤 차이가 있는가?
3. 검증 피드백에 의한 Profile 교정이 데이터 손실 없이 오류를 줄이는가?
4. MIMIC에서 확정한 방법이 Synthea의 다른 스키마에서도 작동하는가?

| 군 | 준비 단계 | 평가 단계 |
| --- | --- | --- |
| B1 Direct | 공통 프롬프트 확정 | 동일 원천 행에서 LLM이 FHIR 직접 생성 |
| P1 Hybrid | Mapping Agent의 초기 Profile 생성 | 고정 초기 Profile을 Renderer로 실행 |
| P2 Hybrid + Correction | 준비 환자에서 Profile 자동 교정 | 고정 교정 Profile을 Renderer로 실행 |

P2의 교정은 test 실행 전에 끝납니다. 평가 정답을 보고 Profile이나 프롬프트를
수정하는 실험은 별도 적응 실험으로 분류합니다. RAG ablation은 개발 단계에
별도 config로 선언하고 각각 정책을 잠근 뒤 같은 분할·입력으로 비교합니다.

## 데이터와 환자 분할

| 데이터 | 역할 | 기본 비율 | 사용 목적 |
| --- | --- | ---: | --- |
| MIMIC | development | 60% | 프롬프트·검색·교정 정책 개발, Profile 준비 |
| MIMIC | validation | 20% | 개발 중 설정 선택, 최종 Profile 준비 가능 |
| MIMIC | test | 20% | 정책 고정 후 내부 평가 |
| Synthea | calibration | 20% | 고정 Agent로 Synthea Profile 생성·자동 교정 |
| Synthea | test | 80% | 합성 외부 평가 |

환자 ID 목록은 JSON 배열입니다. 정수와 문자열을 보존하며 중복 키는 거부합니다.
분할은 seed와 SHA256 순위로 재현하고 입력 순서에 영향을 받지 않습니다.
동일 환자의 모든 Encounter·임상 이벤트는 같은 분할에 속합니다. 작은 pilot에서는
일부 역할이 비어 있을 수 있으므로 실제 연구 전에 환자 수와 역할별 수를 확인합니다.

소규모 회귀 검증에는 MIMIC과 Synthea 각각 별도의 고정 환자 집합을 사용합니다.
임의로 환자를 다시 뽑거나 임상 이벤트의 일부를 잘라서 검증하지 않습니다.
준비·평가 파일과 잠금 파일을 권한이 제한된 로컬 `artifacts/` 아래 보관합니다.
파일럿의 오류를 보고 기준을 조정했다면, 같은 환자를 본평가에서 처음 보는 환자로
취급하지 않습니다.

Synthea는 같은 생성 실행의 CSV를 입력, FHIR R4를 독립 참조로 준비합니다.
생성 버전·seed·기준일·모듈·export 설정을 별도 기록하고 추출 명세의 generator
metadata에도 반영합니다. `dataset_version: unspecified`를 실제 생성 버전으로
교체해야 하며 `R4`를 generator 버전으로 쓰지 않습니다.

Synthea 결과를 확인한 뒤 프롬프트·Renderer·검색·교정 정책을 바꾸면 새로운
개발 실험입니다. 기존 외부평가 결과와 혼합하지 않고 새 lock·실험 ID를 만듭니다.
실제 타 병원 데이터 검증을 수행한 것으로 해석하지 않습니다.

## 입력 준비 원칙

데이터셋별 테이블·컬럼·환자 필터는 `research/fhir_profile/data/specs/`에서
관리합니다. 이 명세는 자동 추론 대상이 아니므로 수동 준비 범위를 보고합니다.
기본 명세는 임상 이벤트 LIMIT를 두지 않습니다. DB 추출기는 동일 필터 COUNT와
실제 추출 수를 기록하므로 custom limit 사용 시 `truncated`를 확인해야 합니다.

Synthea CSV는 헤더만 lowercase로 정규화하고 원래 헤더 대응표를 저장합니다.
값은 원문 문자열로 보존하며 숫자값 `12.30`, UUID, 날짜와 offset, 빈 단위를
추출 단계에서 해석하지 않습니다. `TYPE`, `CATEGORY`, `SYSTEM` 등 의미 필드를
보존하고 `_source_record_id`와 파일 SHA256으로 원천 행을 추적합니다.

## 소규모 고정 코호트 검증

역할별 환자 ID 배열을 담은 JSON을 데이터셋마다 따로 준비합니다. MIMIC의
역할은 `development`, `validation`, `test`이며, Synthea의 역할은
`calibration`, `test`입니다. 한 파일 내에서 역할 간 환자 목록은 겹치지 않아야
합니다. 준비 역할 중 적어도 하나와 평가 역할에는 환자가 있어야 합니다. 작은
MIMIC 코호트에서는 사용하지 않는 `validation`을 빈 배열로 둘 수 있습니다.
ID 목록은 코드에 넣거나 Git에 올리지 않습니다.

```bash
python main.py research split --dataset mimic_iv \
  --fixed-roles artifacts/fixed/mimic-roles.json --output artifacts/fixed/mimic-split.json
python main.py research split --dataset synthea \
  --fixed-roles artifacts/fixed/synthea-roles.json --output artifacts/fixed/synthea-split.json
```

각 split을 아래 [추출 명령](#실행-순서)의 `--split`으로 지정해 준비·평가 레코드를
저장합니다. 아래 예시에서는 `--output`이 각각 `artifacts/fixed/mimic-preparation.json`,
`artifacts/fixed/mimic-test.json`, `artifacts/fixed/synthea-preparation.json`,
`artifacts/fixed/synthea-test.json`이라고 가정합니다. 평가 참조는 같은 고정 평가
환자의, 평가 계약에 맞는 별도 FHIR 파일 또는 디렉터리로 준비합니다.

```bash
for dataset in mimic synthea; do
  python main.py research cohort-lock \
    --preparation "artifacts/fixed/$dataset-preparation.json" \
    --records "artifacts/fixed/$dataset-test.json" \
    --config experiments/configs/pilot.json \
    --reference "artifacts/fixed/$dataset-reference.json" \
    --output "artifacts/fixed/$dataset-cohort.lock.json" || break
done
```

재실행 전에 같은 파일을 다음과 같이 확인합니다.

```bash
for dataset in mimic synthea; do
  python main.py research cohort-verify \
    --cohort-lock "artifacts/fixed/$dataset-cohort.lock.json" \
    --preparation "artifacts/fixed/$dataset-preparation.json" \
    --records "artifacts/fixed/$dataset-test.json" \
    --config experiments/configs/pilot.json \
    --reference "artifacts/fixed/$dataset-reference.json" || break
done
```

독립 정답 매핑이 준비됐다면 잠금 생성과 재확인에 동일한 `--reference-profiles`를
전달합니다. 환자·원천 입력 또는 평가 참조가 다르면 기존 실행과 섞지 말고 차이를
확인한 후 새 실행으로 기록합니다. `--config`에 지정된 데이터셋별 참조 설정은
참조 FHIR를 불러올 때 적용합니다. 고정 파일럿의 `generate-profile`과 `evaluate`에도
각 데이터셋의 `--cohort-lock`을 전달합니다. 이 잠금은 전체 실행 설정, 모델, 코드,
인덱스 등을 고정한 정책 `--lock`과 별개입니다.

코호트 잠금 확인은 환자 및 입력의 재현성을 확인하는 절차입니다. 저장된 Profile과
실제 HL7 Validator로 다시 실행한 결과는 새 LLM 생성·보정 실험으로 집계하지
않습니다. 각각에 사용한 명령과 실제 실행된 구성요소를 [검증 기록](./research_verification.md)에
남깁니다.

## 실행 순서

아래 경로는 사용자가 준비할 데이터·도구의 예시입니다. 데이터 다운로드,
Synthea 실제 생성, 원천 DB 접속, 실제 모델 추론 결과를 의미하지 않습니다.
Ollama·모델·임베딩 모델, Java와 Validator jar를 먼저 준비하고
`experiments/configs/pilot.json`의 URL·모델·프롬프트·스키마·Validator 경로를
실제 환경에 맞춥니다. 연구 명령은 운영 `.env`의 행 상한을 사용하지 않습니다.
DB 추출을 할 때만 지정한 환경변수의 접속 URL을 읽습니다.

```bash
# 1. 준비한 환자 ID JSON 배열을 재현 가능한 분할로 고정
python main.py research split \
  --patients data/mimic-patient-keys.json --dataset mimic_iv \
  --output artifacts/splits/mimic.json
python main.py research split \
  --patients data/synthea-patient-keys.json --dataset synthea \
  --output artifacts/splits/synthea.json

# 2. MIMIC 준비·평가 cohort를 검토된 명세로 추출
python main.py research prepare \
  --spec research/fhir_profile/data/specs/mimic_iv.json \
  --split artifacts/splits/mimic.json --role development \
  --db-url-env SOURCE_DB_URL --output artifacts/cohorts/mimic-development.json
python main.py research prepare \
  --spec research/fhir_profile/data/specs/mimic_iv.json \
  --split artifacts/splits/mimic.json --role test \
  --db-url-env SOURCE_DB_URL --output artifacts/cohorts/mimic-test.json

# 3. 개발 종료 후 FHIR 지식 index와 최종 정책 고정
python main.py research build-rag \
  --config experiments/configs/pilot.json --output artifacts/fhir-index.json
python main.py research freeze \
  --config experiments/configs/pilot.json --index artifacts/fhir-index.json \
  --output artifacts/policy.lock.json

# 4. MIMIC 초기·교정 Profile 생성 후 내부 평가
python main.py research generate-profile \
  --lock artifacts/policy.lock.json --index artifacts/fhir-index.json \
  --records artifacts/cohorts/mimic-development.json \
  --output artifacts/profiles/mimic.json
python main.py research evaluate \
  --lock artifacts/policy.lock.json --index artifacts/fhir-index.json \
  --records artifacts/cohorts/mimic-test.json --profiles artifacts/profiles/mimic.json \
  --reference data/reference/mimic-test \
  --groups B1 P1 P2 --output-dir artifacts/results/mimic-test-01

# 5. 같은 정책에서 Synthea 준비·평가 데이터를 분리 추출
python main.py research prepare \
  --spec research/fhir_profile/data/specs/synthea.json \
  --split artifacts/splits/synthea.json --role calibration \
  --csv-dir data/synthea/csv --output artifacts/cohorts/synthea-calibration.json
python main.py research prepare \
  --spec research/fhir_profile/data/specs/synthea.json \
  --split artifacts/splits/synthea.json --role test \
  --csv-dir data/synthea/csv --output artifacts/cohorts/synthea-test.json

# 6. Synthea용 별도 Profile 생성·합성 외부평가
python main.py research generate-profile \
  --lock artifacts/policy.lock.json --index artifacts/fhir-index.json \
  --records artifacts/cohorts/synthea-calibration.json \
  --output artifacts/profiles/synthea.json
python main.py research evaluate \
  --lock artifacts/policy.lock.json --index artifacts/fhir-index.json \
  --records artifacts/cohorts/synthea-test.json --profiles artifacts/profiles/synthea.json \
  --reference data/reference/synthea-test \
  --groups B1 P1 P2 --output-dir artifacts/results/synthea-test-01
```

`python main.py research`는 `python -m research.fhir_profile`과 같은 연구 명령을
실행합니다. 출력 경로는 실행마다 새로 지정하고 기존 결과와 섞지 않습니다.
`build-rag`, `freeze`, `generate-profile`, B1 평가는 실제 Ollama 접근이 필요합니다.
실제 모델 digest를 확인하지 못하면 정책을 잠그지 않습니다.

## 정책 잠금과 데이터 누출 방지

lock에는 프롬프트 원문·설정, 모델 digest, 코드 hash, FHIR Schema hash, RAG index와
임베딩 모델 식별자, Validator jar hash를 기록합니다. 모델·코드·index·schema가
변하면 평가를 거부하고 새 개발 실험으로 다시 고정해야 합니다. DB 비밀번호는
lock에 넣지 않고 환경변수로 전달합니다.

Profile에는 준비 cohort와 split hash·환자 목록을 기록합니다. 준비 환자와 평가
환자가 겹치거나 다른 dataset의 Profile을 사용하면 거부합니다. FHIR index에는
FHIR 정의만 저장하며 환자 데이터·참조 FHIR·정답 매핑을 넣지 않습니다.
이중 RAG의 원천 index에도 추출 명세의 허용 메타데이터만 넣습니다. 전체 정책은
두 데이터셋이 공유하고, 데이터셋별 원천 index는 생성된 Profile과 평가 기록에 고정합니다.

`generate-profile`에는 참조 정답 인수가 없습니다. `evaluate --reference`에서만
독립 참조를 읽고 최종 결과와 비교합니다. 정답 기반 오류를 Agent의 교정 입력에
사용하지 않습니다.

## 독립 참조 평가

Synthea 원본 FHIR 전체를 바로 점수화하지 않습니다. 고정한 test 환자와 연구
리소스 4종에 해당하는 공통 범위를 골라 별도 평가용 참조를 준비해야 합니다.
입력 CSV가 표현하지 않는 필드는 비교 대상에서 제외하고 제외 기준을 문서화합니다.

Patient·Encounter는 정책의 `evaluation.reference_by_dataset`에 identifier system을
지정하여 원천 업무키로 ID와 참조를 정렬합니다. Condition·
Observation은 환자·Encounter·code·시간을 기본 이벤트 키로 비교하고 반복 이벤트
개수를 보존합니다. exporter의 `urn:uuid:` 참조, 상대 참조, ID namespace가 다르면
같은 identifier 정렬 규칙을 적용합니다. exporter별 코드·시간 해상도 및 이벤트
범위는 평가 전에 검토하고 config에 고정합니다.

기본 필드 비교는 참조의 임상 leaf 값을 사용합니다. 연구 대상 필드 범위를
`evaluation.field_paths`에 명시해 고정하는 것을 권장합니다. ID가 일치한다는
이유로 모든 값이 맞다고 판정하거나, 예측 Profile로 정답 FHIR를 만들지 않습니다.
독립 참조가 없으면 표준·개수 검사는 가능하지만 원천 충실도와 종합 합격은
미평가입니다.

기본 pilot 설정은 Condition의 `onsetDateTime`을 양쪽 모두 날짜 단위로 투영해
비교합니다(`evaluation.date_only_paths`). 이는 CSV의 START가 날짜까지만
표현하기 때문이며, 해당 지표가 시각까지 보존함을 의미하지 않습니다. 원본 FHIR
출력은 바꾸지 않고 비교용 사본에만 적용하며, 이 규칙도 정책 잠금에 포함됩니다.
Observation status는 원천에 상태 필드가 없으므로 기본 충실도 비교 범위에서
제외하고 표준 적합성 검사로 검증합니다. 필요한 다른 범위는 개발 단계에 정한 뒤
잠그며 Synthea 정답을 보고 사후에 제외 항목을 늘리지 않습니다.

MIMIC의 기본 추출 명세에는 birthDate·Encounter class·coding system 원천이
없습니다. pilot의 `evaluation.field_paths_by_dataset.mimic_iv`는 이러한
미제공 필드와 MIMIC-on-FHIR의 파생값을 충실도 점수에 넣지 않습니다. 대신
실제 제공된 값·코드·단위·시간·참조 보존을 비교하며, 표준 적합성 검사는
계속 전체 생성 리소스에 적용합니다. 데이터셋별 평가 범위도 같은 lock에
포함되므로 두 데이터셋의 충실도 절대값을 동일 난이도 점수로 해석하지 않습니다.

본평가용 계약에는 MIMIC 시간대 없는 원천 시각의 해석과 모호한 시각의 처리,
Synthea의 패널 구성요소와 CSV 검사 행의 대응 범위, `value[x]` 자료형의 선택,
필수 원천 참조의 기대 대상 및 식별자 정렬 규칙을 명시합니다. 참조 버전이나
내보내기 방법이 달라지는 경우에는 이전 계약이 그대로 유효하다고 가정하지 않습니다.
패널 구성요소를 비교 단위에 포함하더라도 Bundle의 최상위 Observation 수와는
구분해서 보고합니다.

`evaluation.projections_by_dataset`은 평가용 사본에만 적용합니다. 현재 Synthea
설정의 `component_events`는 구성요소가 있는 부모 Observation 대신 각 구성요소를
비교합니다. 환자·입원 참조와 검사 시각은 부모에서 가져옵니다. `copy_paths`는
원천에 문자열로 표현된 텍스트를 비교할 때 참조의 `valueCodeableConcept.text`를
평가용 `valueString`과 대응시킵니다. 이 투영은 실제 FHIR 출력과 Validator 입력을
바꾸지 않습니다. 구성요소 투영을 적용해도 원천 행과 참조의 모든 이벤트가
대응한다고 가정하지 않고, 매칭되지 않은 수를 별도로 보고합니다.

## 지표와 보고 원칙

| 영역 | 기록할 내용 |
| --- | --- |
| 표준 적합성 | JSON Schema 및 실제 HL7 Validator 통과 여부·error/warning |
| 생성 완전성 | 원천 행 수, 생성 수, 누락·과생성, 리소스별 이벤트 Precision/Recall/F1 |
| 원천 충실도 | 독립 참조 대비 값·코드·단위·시간·상태 정확도. 이벤트 매칭 실패를 포함하는 전체 필드 정확도와 매칭 성공 이벤트만의 필드 정확도를 분리 |
| 참조 무결성 | 실제 대상 존재·중복 ID·환자 소속 일치. 원천상 기대된 참조의 누락·오연결은 생성된 참조 해소율과 별도로 확인 |
| 매핑 품질 | 독립 참조 매핑 대비 필드·타입·참조 Precision/Recall/F1 |
| 실행 일치성 | 승인 Profile과 Renderer 출력의 일치성; 정확도와 별도 보고 |
| 교정 효과 | 준비 단계 전후 오류 수·채택/거부·복구·신규 오류·정보 손실 |
| 처리 비용 | Profile 준비 및 평가의 실제 호출·토큰·시간; 준비비 포함 총비용 |

`event_match_coverage`는 전체 참조 이벤트 대비 매칭된 이벤트의 비율입니다.
`matched_event_field_accuracy`는 매칭된 이벤트의 비교 가능 필드만 분모로 삼습니다.
두 값을 전체 범위의 `source_field_accuracy`와 함께 보고합니다. 예를 들어 이벤트의
절반만 매칭됐지만 그 안에서 값이 모두 맞으면, 조건부 정확도는 100%여도 전체
보존이 성립한 것은 아닙니다. `expected_reference_metrics`는 평가 범위에 있고
독립 참조 FHIR에 실제 값이 있는 Reference에 대해 생성 여부(`coverage`)와 대상이
올바른지(`target_accuracy`)를 구분합니다. 매칭되지 않은 이벤트의 기대 참조는
누락으로 셉니다. 생성된 참조의 해소율은 별도 검사입니다.

독립 참조 매핑은 `evaluate --reference-profiles`로 전달합니다. 평가 manifest는
환자별 결과와 B1/P1/P2 대응 bootstrap 구간, Profile 보정 성공률 및 준비비를
포함한 비용을 기록합니다. RAG 대응 결과는 `compare-results --rag-ablation`으로
비교합니다. `analysis.target_half_width`는 pilot 대응 차이의 평균 정밀도를 위한
표본수 근사를 제어합니다. 본실험 검정력 설계와 최종 논문 표는 별도 분석합니다.
참조가 없는 지표를 100%로 보고하지 않고, 전체 시도 환자를 포함한 성공률과
생성된 Bundle만의 통과율을 구분합니다.

`--reference-profiles`의 입력은 리소스 종류를 키로 하는 독립 Profile 객체입니다.
평가 입력의 데이터셋·테이블·메타데이터에 맞아야 하며 예측 규칙을 복사해 만들지
않습니다. 현재 매핑 점수의 자료형·연산 비교는 정규화한 연산 설정의 일치 여부를
측정합니다. 설정이 달라도 동일한 출력을 만드는 규칙을 허용할 경우에는 사전에
별도의 동등성 판정 기준과 검증 예를 정합니다.
[파일럿 참조 매핑](../experiments/references/README.md)은 평가 경로를 확인하는
부분 참조로, 본실험의 전문가 검토 정답과 구분합니다.

MIMIC와 Synthea의 절대 점수 차이를 곧바로 전이 손실로 해석하지 않습니다.
데이터셋별 B1 대비 P1/P2 차이를 먼저 비교하고, 환자 단위 paired bootstrap 등으로
신뢰구간을 산출합니다. 환자 내 다수 Observation을 독립 환자처럼 세지 않습니다.

실험마다 코드 commit/hash, 정책·index·Profile·split·원천 cohort·참조 hash,
모델 digest, 생성기 설정, 실행 환경, 실패 사례, 수동 개입량을 보존합니다.
기존 `fhir_export*` 결과는 과거 운영 파이프라인의 예비 결과이며 이 프로토콜의
Direct/Hybrid 비교 또는 Synthea 외부검증 결과로 사용하지 않습니다.

원천 보존 assertion과 ID 정렬 설정, 출력 지표의 정의는
[구현 안내](./research_implementation.md), 검증 범위는
[검증 기록](./research_verification.md)을 참고합니다.
