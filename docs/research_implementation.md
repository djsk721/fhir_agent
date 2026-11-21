# 연구계획 구현과 검증

## 구현 범위

첨부 연구계획의 수정 메모를 기준으로 고정 Data Extractor, Local LLM Mapping
Agent, FHIR Schema RAG, Conversion Profile, 결정적 Renderer, Profile 보정,
동일 원천 행의 B1/P1/P2 평가를 제공합니다. 연구 명령은
`python main.py research --help`에서 확인합니다.

실제 Ollama Profile 생성·수정 호출은 Conversion Profile JSON Schema를
`format`에 전달합니다. 대상 Resource의 FHIR root, 실제 원천 컬럼, dataset/table을
동적으로 제한합니다. 응답은 클라이언트의 JSON Schema 검사와 전체 FHIR
경로·타입·choice 검사로 재검증합니다. 토큰 상한에서 절단된 응답은 거부합니다.
디코더용 스키마는 속성 순서를 고정하며, 호출 이력에는 디코더용과 검증용
스키마의 해시를 각각 기록합니다.

GPU 배치를 명시해야 할 때 연구 config의 `model.num_gpu`와 `rag.num_gpu`를
지정합니다. `0`은 각각 생성과 임베딩 요청의 CPU 실행을 지정합니다.
선택값은 정책 잠금과 모델 호출 기록에 포함됩니다. 생략 시 Ollama의 배치
정책을 따릅니다.

| 연구 요구 | 구현과 산출물 |
| --- | --- |
| 원천 값 기반 보정 | 정책에 고정한 `source_checks`를 준비 데이터에 적용 |
| 보정으로 발생한 오류 | 오류 multiset의 해결·신규·잔여 수와 후보 채택 이력 |
| 독립 참조 정렬 | identifier system별 Patient/Encounter ID 및 상대·fullUrl 참조 정렬 |
| 매핑 정확도 | 평가 전용 `--reference-profiles`의 필드·타입·참조 Precision/Recall/F1 |
| 표준 적합성 | 전체 참조 구조 검사와 환자별 HL7 Validator 결과, 환자 Bundle 통과율 |
| 대응 비교 | 같은 환자의 B1/P1/P2 차이와 환자 단위 bootstrap 95% 구간 |
| RAG 유무 비교 | `compare-results --rag-ablation`의 동일 실험 조건 검사 |
| Pilot 분석 | 환자별 이벤트 수, 실패 수, 대응 차이 분산 및 CI 정밀도 기반 환자 수 근사 |
| 처리 비용 | 준비·평가·합산 chat/embedding 호출, 토큰 및 실행시간 |

## 원천 보존 assertion

`source_checks`는 dataset 및 resourceType별 배열이며 Conversion Profile과 같은
선언적 연산을 사용합니다. 생성 Profile의 매핑을 복사해 기대값을 만드는 대신,
실험 준비 단계에 승인한 원천 의미 계약으로 기대값을 계산합니다.

```json
{
  "source_checks": {
    "example_dataset": {
      "Observation": [
        {
          "id": "preserve-number",
          "target_path": "valueQuantity.value",
          "operation": "decimal",
          "source_field": "result_number",
          "when": {"field": "result_number", "present": true}
        }
      ]
    }
  }
}
```

pilot 설정에는 성별, 검사 코드 및 숫자/문자 검사값의 제한된 assertion을
포함합니다. 이는 데이터셋별로 사람이 정한 사전 지식이며 수동 개입량으로
보고해야 합니다. 검사하지 않은 필드의 충실도는 독립 참조 평가로 확인합니다.
준비 단계의 assertion 충족률은 독립 참조 정확도와 각각 기록합니다.
assertion이 없으면 보존 검사는 미평가로 기록합니다.

조건이 거짓인 assertion은 해당 choice/선택 필드의 부재를 검사합니다.
원천 값이 없는 경우의 `null_policy`와 원천 timestamp의 timezone은 명시적으로
정의합니다. 승인되지 않은 코드체계나 시간대의 추정은 Profile 보정 기준에
포함하지 않습니다.

보정 후보는 기존 차단 오류가 줄고 신규 차단 오류가 없을 때 채택합니다.
원천 필드 삭제는 거부하며, 원천 보존 검사가 실패한 동일 target의 잘못된
컬럼을 교체하는 경우에는 보존 assertion의 재검증으로 허용 여부를 판단합니다.
수정안의 파싱·Profile 검증 실패는 거부 이력에 저장하고 설정된 보정 횟수 내에서
그 오류를 다음 요청에 전달합니다. 원천 데이터 사전의 `column_descriptions`와
기본 FHIR 필드 설명·enum을 두 비교군에 동일하게 제공합니다.
보정 요청에는 반복 오류를 유형별 건수와 최대 두 개 예시로 전달합니다.
원천 보존 오류의 예시에는 기대값과 실제값을 포함합니다. 생성 초안이
계약을 위반하면 해당 초안과 오류를 다음 요청에 제공합니다.

`generation.timezone`은 timezone 없는 원천 timestamp의 명시적 해석 정책입니다.
이미 offset이 있는 값은 그 offset을 보존합니다. 검증에서는 운영 `.env`에
명시된 `FHIR_DEFAULT_TIMEZONE`을 연구 config에 복사하고 lock에 기록했습니다.

`history[].candidate_issue_changes`는 후보의 해결·신규 오류를 기록합니다.
`resolved_error_count`와 `introduced_error_count`는 최종 채택 결과를 기준으로,
`proposed_introduced_error_count`는 거부된 후보까지 포함해 계산합니다.
`correction_summary.repair_success_rate`의 단위는 초기 검증에 실패한
리소스별 Profile이며, 개별 Observation 행의 복구율과 구분해 표기합니다.

## 독립 FHIR 참조와 매핑 정답

실험 config의 `evaluation.reference_by_dataset`에 다음 정보를 지정합니다.

- `file_patterns`로 평가 대상 파일을 선택합니다.
- `identity_rules.<resourceType>.identifier_system`으로 원천 업무키에 대응하는
  identifier를 선택합니다.
- 선택적으로 `prefix`를 선언해 사전 고정한 ID namespace를 적용합니다.

MIMIC pilot에는 Patient와 입원 Encounter의 identifier URI 및 핵심 4개 파일을
지정했습니다. 시스템별 식별자가 없거나 여러 값으로 모호한 경우 평가를
실패로 처리합니다. Patient·Encounter ID와 이를 가리키는 상대 참조 및 Bundle
fullUrl 참조를 함께 정렬합니다. 파일의 원본 값은 유지합니다.

독립 매핑 정답 JSON은 resourceType을 key로 하고 Conversion Profile을 value로
가집니다. 평가자가 따로 검토한 매핑을 준비하고 평가 명령에 전달합니다.

```bash
python main.py research evaluate \
  --lock artifacts/policy.lock.json --index artifacts/fhir-index.json \
  --records artifacts/cohorts/mimic-test.json \
  --profiles artifacts/profiles/mimic.json \
  --reference /path/to/mimic/fhir \
  --reference-profiles data/reference/mimic-mappings.json \
  --groups B1 P1 P2 --output-dir artifacts/results/mimic-test
```

참조 FHIR와 매핑 정답은 평가 단계에서만 읽습니다. Profile 생성·교정 명령의
입력에는 포함하지 않습니다. 독립 매핑을 제공하지 않은 지표는 미평가입니다.

## 환자별 평가와 통계

각 비교군의 `patients`에는 모든 평가 환자의 원천 수, 생성 수, 표준 검증,
원천 충실도, 이벤트 F1과 종합 합격 여부가 기록됩니다. 누락된 생성 결과도
평가 환자 목록에 남습니다. 환자에 소속시킬 수 없는 생성 리소스는 전체
검증 오류로 기록합니다.

각 환자의 리소스로 Bundle을 만들어 HL7 Validator를 실행합니다. 전체 cohort에는
별도로 구조·중복 ID·참조 검사를 수행합니다. 환자 수가 많을수록 Java 실행
시간도 증가하므로 `validation.timeout_seconds`를 실행 환경에 맞게 설정합니다.
표준 적합성 결과는 `external_validation_status`로 완료·timeout·미실행을
구분합니다. Validator 실행 오류는 실행 상태에 남고 FHIR 리소스 오류 건수에는
합산하지 않습니다.

`paired_comparisons`는 오른쪽 군에서 왼쪽 군을 뺀 환자별 차이를 사용합니다.
bootstrap seed와 재표집 횟수는 config의 `analysis`에 고정합니다. 환자 1명이면
평균 차이만 기록하고 신뢰구간은 미평가입니다. 대응 환자의 지표가 미평가이면
해당 비교도 미평가로 남깁니다.

`analysis.target_half_width`를 지정하면 pilot에서 관찰한 환자별 충실도 차이의
분산으로 평균 차이의 95% 구간 정밀도를 위한 표본수를 근사합니다. 분산이
0이거나 환자가 부족하면 추정값은 비워 둡니다. 본실험의 가설검정 검정력,
목표 효과크기 및 다중비교 계획은 연구자가 별도로 정해야 합니다.

## RAG 대응 비교

RAG 활성/비활성 config는 `rag.enabled`만 바꿔 각각 freeze하고 동일 split과
cohort로 실행합니다. 다음 명령은 모델 digest·코드·참조·cohort 및 나머지
설정의 일치를 검사한 후 대응 차이를 계산합니다.

```bash
python main.py research compare-results \
  --left artifacts/results/no-rag/manifest.json \
  --right artifacts/results/rag/manifest.json \
  --rag-ablation --seed 42 --resamples 2000 \
  --output artifacts/results/rag-comparison.json
```

RAG 활성 실행이 오른쪽이면 양수 차이는 해당 지표의 향상입니다.

## 비용과 재현

`cost_summary`에는 Profile 준비비와 평가비를 각각 기록하고 합산합니다.
P1에는 초기 Profile 생성비, P2에는 초기 생성과 보정비를 포함합니다.
API 토큰 수가 없는 호출은 토큰 비용을 미평가로 처리합니다. Local LLM의
금전 비용은 가격·전력 측정값이 제공되지 않아 비워 둡니다. RAG 인덱스 구축은
공통 사전 준비 단계이며 index 구축 시간은 개별 비교군 비용에 포함하지 않습니다.

Profile 생성은 출력 파일 옆의 `.checkpoints/`에 초기·보정 리소스별 기록을,
평가는 결과 디렉터리 옆의 `.checkpoints/`에 비교군별 기록을 저장합니다.
체크포인트의 `complete=false`는 중간 상태를 뜻합니다. 최종 판정에는 전체
`manifest.json`을 사용합니다. JSON은 임시 파일에서 완성한 뒤 원자적으로 교체합니다.
Direct 생성은 한 배치가 실패해도 다른 배치의 성공 결과와 오류를 함께 보존합니다.

`test.ipynb`는 연구 CLI를 호출하는 단계별 실행 노트북입니다. 기본 RUN 플래그는
꺼져 있으며 config와 기존 결과를 조회합니다. 실제 실행에는 단계별 플래그를
켜고 새 산출물 경로를 지정합니다.

검증 실행 결과와 남은 실험 항목은 [검증 기록](./research_verification.md)에 기록합니다.
