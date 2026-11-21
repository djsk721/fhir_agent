# 연구 구현 검증 기록

검증은 2026-11-16부터 2026-11-20까지 진행했으며 현재 작업 트리의 미커밋
변경사항을 포함합니다. 초기 실행과 재실행은 날짜별 검증 디렉터리에 보존합니다.

## 자동 검증

| 항목 | 결과 |
| --- | --- |
| 전체 테스트 | `python -m pytest -q` 실행, 136개 통과 |
| Python 구문 | `python -m compileall -q fhir_agent_pipeline research main.py` 통과 |
| Notebook 형식 | nbformat 검증 통과 |
| Notebook 기본 실행 | 실제 Jupyter 커널에서 코드 셀 5개 통과 |
| 정책 잠금 | 실제 모델 digest 및 코드 hash 재검증 통과 |
| FHIR RAG | 실제 Ollama 임베딩으로 문서 11,504개 인덱스 생성 |

추가 회귀 테스트는 다음 사례를 확인합니다.

- FHIR 형식이 유효한 오매핑을 독립 원천 assertion으로 탐지·보정
- assertion이 확인한 잘못된 원천 컬럼의 교체
- 총 오류 수가 감소해도 신규 오류가 발생한 후보의 거부
- 같은 오류가 반복된 경우의 해결 수 보존
- 수정안 파싱 실패 시 기존 결과 및 거부 이력 보존
- identifier 기반 Patient/Encounter 및 fullUrl·상대 참조 정렬
- 모호한 identifier의 거부
- 환자 누락을 제외하지 않는 대응 비교와 환자 1명의 구간 미평가
- RAG 이외 설정이 다른 ablation 실험의 거부
- 독립 매핑 평가 및 준비비·평가비 집계의 실제 실험 runner 연결
- Validator 출력 절단 이전 오류 파싱과 line/column 위치 변동 정규화
- 모델 연결 실패의 산출물 보존과 미생성 Bundle 통과율 0 처리
- 구조화 Profile 출력 스키마의 필수 operation 필드 및 모델 호출 연결

## 실제 MIMIC 읽기 전용 검증

기존 export의 양수 Observation 수가 작은 환자를 demo cohort와 교차하여 2명
선정했습니다. 각 환자의 전체 원천 행을 추출했으며 이벤트 LIMIT는 적용하지
않았습니다. `selection.json`과 `split.json`에 선정 기준과 분할을 기록했습니다.

| 분할 | Patient | Encounter | Condition | Observation | 합계 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 준비 환자 1명 | 1 | 1 | 4 | 89 | 95 |
| 평가 환자 1명 | 1 | 1 | 15 | 66 | 83 |

원본 MIMIC-on-FHIR 파일을 새 identifier 정렬 규칙으로 읽었습니다. 평가 환자의
참조 리소스 수는 위의 원천 수와 일치했으며 환자·입원 ID를 업무키로 정렬했습니다.
근거 파일은 `reference-alignment.json`입니다.

MIMIC의 원천 임상값을 `http://localhost:11434` Ollama에 전달하는 단계는 자동
승인 검토에서 처음 차단됐습니다. 이후 사용자가 해당 demo 환자 payload를 이
호스트의 localhost Ollama로 전달하는 것을 명시적으로 승인했습니다. 승인 후
구조화 출력 보완 버전의 MIMIC Profile 생성·보정을 실행했습니다.

## 실제 모델·Validator 통합 검증

`synthetic-fixture/`에는 사람이 만든 민감정보 없는 환자 3명의 검증 입력과 독립
참조를 저장했습니다. 준비 환자 1명과 평가 환자 2명으로 분리하며, 각 환자는
Patient·Encounter·Condition 1건씩과 숫자·문자 Observation 2건을 가집니다.
입력 스키마는 Synthea 추출 계약을 사용하고 생성기 metadata에 수작업 fixture임을
기록했습니다. 이 입력으로 소프트웨어 연결과 실제 Local LLM 실행을 확인합니다.

초기 합성 fixture의 자유 JSON Profile 생성은 실제 모델 호출 8회, 693.5초가
소요됐고 4종 모두 operation 필드 계약 오류로 거부됐습니다.
`synthetic-fixture/no-rag.profiles.json`에 실제 호출·오류를 보존했습니다.
진행 중인 초기 RAG fixture 검증은 코드 보완을 위해 중단했습니다.

이 결과를 반영해 Mapping Agent의 실제 Ollama 호출에는 dataset 독립적인
Conversion Profile JSON Schema를 적용했습니다. 필수 `source_field`, reference의
`resource_type`, 중첩 coding part 구조를 모델 출력 단계부터 제한하고 기존
Profile 검증으로 다시 검사합니다. 검증 환경의 Ollama는 GPU 메모리 사용량이
0으로 확인됐으며 CPU 추론을 수행했습니다.

독립 참조는 생성 Profile을 사용하지 않고 fixture 원천 의미로 따로 작성했습니다.
실행은 `gemma4:e4b`, temperature 0, seed 42, context 16,384를 사용합니다.
FHIR R4 Validator는 `terminology_server=n/a`로 실행하여 구조 검증을 수행합니다.
이 설정에서는 원격 terminology 서버에 의한 코드 유효성 검증 범위를 별도로
고려해야 합니다. Validator timeout은 검증용 config에서 90초로 지정했습니다.

재실행은 새 출력 경로를 지정한 뒤 다음 순서로 수행합니다.

```bash
python main.py research freeze \
  --config artifacts/verification-20261116/synthetic-fixture/no-rag.config.json \
  --output artifacts/recheck/no-rag.lock.json

python main.py research generate-profile \
  --lock artifacts/recheck/no-rag.lock.json \
  --records artifacts/verification-20261116/synthetic-fixture/calibration.json \
  --output artifacts/recheck/no-rag.profiles.json

python main.py research evaluate \
  --lock artifacts/recheck/no-rag.lock.json \
  --records artifacts/verification-20261116/synthetic-fixture/test.json \
  --profiles artifacts/recheck/no-rag.profiles.json \
  --reference artifacts/verification-20261116/synthetic-fixture/reference.json \
  --output-dir artifacts/recheck/no-rag-results
```

RAG 활성 실행은 대응 config와 인덱스를 사용합니다. 대응 결과는
`research compare-results --rag-ablation`으로 계산합니다.

## 2026-11-18 재검증

11월 17일 실행에는 준비 단계 Profile이 남아 있고 완성된 평가 manifest는 없었습니다.
Observation 보정에 반복 오류 268건이 입력됐고 모델 응답이 비어 있었습니다.
재검증 코드에서 다음 사항을 수정했습니다.

- 보정 입력에 오류 유형별 발생 건수와 대표 예시를 전달합니다. 원본 오류와
  보정 결과는 로컬 산출물에 저장합니다.
- FHIR Schema와 실행 필수 필드를 함께 전달하고 Coding·Reference 같은 원소의
  구조를 제공했습니다. 생성할 리소스의 순서는 참조 의존성을 반영합니다.
- 생성 실패 초안과 계약 오류를 다음 요청에 포함합니다. 원천 보존 실패에는
  기대값과 실제값을 담아 의미 보정을 지원합니다.
- HL7 Validator가 구조화 오류를 반환할 때 내부 참조 오류도 함께 처리합니다.
  동일 레코드 검사를 반복 보고한 오류는 중복 집계에서 제외합니다.
- Validator timeout은 `external_validation_status=timeout`으로 기록합니다.
  오류 건수는 실제 Validator가 반환한 진단만 계수합니다. 보정 요청에는
  Profile 변경으로 해결할 수 있는 오류만 전달합니다.

두 MIMIC 비교는 기존의 준비 환자 1명·평가 환자 1명과 로컬
`gpt-oss:20b` 모델을 사용합니다. 같은 코드·모델·데이터·정책에서
`rag.enabled`만 변경합니다. FHIR Validator는 원격 terminology를 비활성화한
상태에서 실행합니다. 최종 재검증은 로컬에 내려받은 FHIR 캐시를 사용하고 JVM의
원격 패키지 조회를 차단했습니다. 평가 데이터의 시간은 잠긴 `Z` 정책으로 처리했습니다.
MIMIC 평가 환자의 Observation 66건은 DB와 참조의 현지 시각이 같고,
참조 쪽에는 `-04:00` offset이 붙어 있습니다. 준비 환자의 참조 시각은
`-05:00`입니다. 두 분할의 검사 155건과 입원 시작·종료 4개 값을
`America/New_York`로 해석하면 참조의 시각과 일치하며, 현재 잠금으로 산출한
점수에는 기존 `Z` 정책과 참조의 차이가 포함됩니다. 원천 ICD 코드의 CHAR
우측 공백은 문자열 변환에서 제거합니다.

### 실제 Synthea 데이터

동일 생성 실행에서 CSV와 FHIR Bundle을 모두 확인한 환자 중 3명을 선택했습니다.
원천 레코드를 모두 추출해 준비 환자 1명과 평가 환자 2명으로 나눴습니다.
선정 방법, 생성기 버전·seed·날짜와 범위는
`artifacts/verification-20261118/synthea/preparation-summary.json`에 기록했습니다.

| 분할 | 환자 | CSV 전체 행 | CSV Observation | 참조 Observation | 참조 Observation component |
| --- | ---: | ---: | ---: | ---: | ---: |
| 준비 | 1 | 119 | 105 | 91 | 16 |
| 평가 | 2 | 322 | 283 | 168 | 89 |

Synthea 참조 Bundle에는 패널과 `valueCodeableConcept`가 포함됩니다. 평가
Observation 283행 가운데 환자·Encounter·코드·시각 키가 참조 최상위 Observation과
일치하는 행은 152건이며 패널 component까지 펼친 진단에서는 241건입니다.
현재 점수는 원본 참조 범위를 사용하므로 exporter 표현 차이가 함께 반영됩니다.

### 초기 Profile 독립 재생

최종 코드로 생성된 초기 체크포인트를 별도로 재생했습니다. 재생 시 모델을
추가 호출하지 않았으며, 평가 원천 전량을 렌더링한 뒤 실제 HL7 Validator를
실행했습니다. 이 진단은 초기 Profile의 상태를 보여줍니다.

| 데이터 | 원천 행 | 변환 리소스 | 렌더링 오류 | HL7 오류 | HL7 경고 |
| --- | ---: | ---: | ---: | ---: | ---: |
| MIMIC 비RAG | 83 | 83 | 0 | 69 | 285 |
| Synthea RAG | 322 | 252 | 70 | 54 | 741 |

MIMIC에서 오류 66건은 Observation 코드체계, 나머지 3건은 Patient 성별과
Encounter 분류 코드체계였습니다. Synthea 평가의 미변환 70건은 Observation
렌더링에서 발생했습니다. 상세 검증 진단은 최종 실행 디렉터리의
`mimic-staged-replay.json`, `synthea-staged-replay.json`에 보존했습니다.

### 저장된 Profile의 P1·P2 재생

11월 18일에 먼저 완성된 Profile을 현재 평가기로 재생했습니다. Profile의
원래 정책 해시와 재생에 사용한 평가기 해시를 manifest에 각각 기록했습니다.
MIMIC 두 군 모두 평가 환자의 원천 83행을 변환했으며, 결과는 다음과 같습니다.

| MIMIC 설정 | P1 HL7 오류 | P2 HL7 오류 | P1·P2 참조 필드 정확도 | P1·P2 이벤트 F1 |
| --- | ---: | ---: | ---: | ---: |
| RAG 활성 | 4 | 2 | 1/427 (0.234%) | 0.0241 |
| RAG 비활성 | 3 | 3 | 1/427 (0.234%) | 0.0241 |

모든 군의 환자 Bundle은 검증에서 실패했습니다. 낮은 참조 점수에는
독립 참조 이벤트 키의 시간대 차이와 Encounter 참조 누락이 반영됐습니다.
두 설정 모두 Condition 15건의 Encounter 참조를 누락했고, 비RAG Observation은
Encounter가 있는 원천 행의 참조도 누락했습니다. 시각의 오프셋만 제거한
진단에서 Observation 이벤트 키 일치는 RAG 66/66건, 비RAG 9/66건이었습니다.
공식 재생 점수에는 같은 오프셋 규칙을 유지했습니다. 재생 결과는
`artifacts/verification-20261118/offline-replays/`에 있고, 비교의 환자 수는
1명이므로 신뢰구간이 비어 있습니다.

### Synthea 최종 정책의 첫 실행

평가 환자 2명에서 P1과 P2는 각각 원천 322행 중 252건을 변환했습니다.
두 군의 HL7 오류는 각각 50건으로 환자 Bundle 통과는 0/2였고,
참조 필드 정확도는 986/1,995 (49.42%), 이벤트 F1은 0.6754였습니다.
문자 Observation 70행은 숫자 변환 중 누락됐습니다. Patient와 Encounter의
Profile이 CSV 행 ID를 생성 키로 선택한 반면 다른 리소스는 원천 업무 키를
참조해 구조 참조 오류가 발생했습니다. 보정이 성공한 준비 Profile은 없었습니다.

첫 Direct 실행은 Ollama 임베딩 조회 단계에서 연결이 거부됐고 모델 생성 호출
0회·출력 0건으로 종료됐습니다. 따라서 해당 B1 값은 서비스 오류로 기록합니다.
원본 manifest는 `artifacts/verification-20261118/synthea/final-results/manifest.json`입니다.

## 2026-11-20 재실행

MIMIC 준비 95행 및 평가 83행은 승인된 두 환자의 파일 해시와 행별 환자 키를
확인했습니다. Ollama는 같은 localhost 주소를 사용했습니다. GPU 재시도에서
Patient와 Encounter의 Profile 생성 요청이 반복해서 CUDA 메모리 할당에
실패했습니다. 당시 GPU의 여유 메모리는 약 1.4~1.5 GiB였으며 다른 프로세스가
두 GPU를 사용하고 있었습니다. 이번 재시도 프로세스를 중단하고 실패
체크포인트를 `artifacts/verification-20261120/mimic/`에 보존했습니다.

환자 데이터 없는 단일 CPU 진단은 같은 모델에서 59.64초에 응답했습니다.
생성과 임베딩의 `num_gpu=0`을 잠금 설정에 추가했습니다. CPU 재실행의 새
정책은 `artifacts/verification-20261120/cpu/`에 보존합니다. 두 설정은 모델,
CPU 배치, 데이터와 검증 정책이 같고 RAG 활성 여부가 다릅니다.

CPU 준비와 평가는 기존 GPU 결과 경로와 별도로 저장합니다. 완료 여부는
`cpu/no-rag-results/manifest.json`, `cpu/rag-results/manifest.json`의
실제 결과로 확인합니다. 초기 Profile 생성의 중간 산출물은
`cpu/no-rag.profiles.checkpoints/`에 있으며, 단독 체크포인트는 실험
완료로 집계하지 않습니다.

비RAG CPU 준비는 1,505.7초, 생성 호출 13회에 완료했습니다. 초기 Profile
4종을 모두 생성했으며 보정으로 채택된 수정안은 0건입니다. 준비 단계의
HL7 및 원천 보존 차단 오류가 남아 모든 보정 Profile의 준비 검증이
실패했습니다. Profile과 개별 진단은 `cpu/no-rag.profiles.json`에 기록했습니다.

비RAG CPU 평가의 P1·P2 체크포인트는 각각 원천 83행을 모두 변환했고,
실제 HL7 Validator는 오류 2건·경고 284건을 반환했습니다. 두 군의
오류 2건은 모두 Patient 성별 필드였습니다. 참조 필드 일치는 두 군 모두
1/427, 이벤트 F1은 0.0241입니다. Direct가 완료되면
세 비교군을 한 manifest에 저장합니다.

RAG CPU 준비는 Patient·Condition·Observation 초기 Profile을 저장했습니다.
Encounter는 모델 응답이 선언적 매핑 계약을 연속 위반하여 초기 생성이
실패했습니다. 실패는 해당 체크포인트에 남겼으며, 평가에도 누락으로 반영합니다.

### GPU 전환

사용자 요청에 따라 CPU의 남은 평가와 대기 작업을 중단하고 산출물을
보존했습니다. GPU 메모리를 다시 확인했을 때 두 RTX 3090의 가용량이 각각
24,123 MiB와 23,801 MiB였습니다. 환자 자료 없는 로컬 추론에서
`gpt-oss:20b`의 모델 메모리 12,870,976,798바이트 전부가 GPU에 배치됐습니다.

GPU 재실행은 `artifacts/verification-20261120/gpu/`에 별도 정책과 결과를
저장합니다. 생성과 임베딩에 `num_gpu=99`를 지정했으며, MIMIC RAG·비RAG
정책은 승인된 동일 자료와 모델을 사용합니다. GPU 실행에서 확보한
manifest로 대응 차이를 계산한 뒤 Synthea 평가가 이어집니다.

MIMIC GPU 준비는 비RAG 139.9초, RAG 112.9초에 완료했습니다. 두 설정
모두 Encounter 초기 Profile 생성이 실패했고, Observation 보정이 준비 오류
89건을 해결했습니다. 준비 검증까지 통과한 보정 Profile은 0종입니다.

| MIMIC GPU | 생성 / 원천 행 | HL7 오류 | 참조 필드 정확도 | 이벤트 F1 |
| --- | ---: | ---: | ---: | ---: |
| 비RAG P1 | 82/83 | 68 | 0.00% | 0.0121 |
| 비RAG P2 | 82/83 | 2 | 0.00% | 0.0121 |
| 비RAG B1 | 75/83 | 20 | 11.01% | 0.2152 |
| RAG P1 | 82/83 | 68 | 10.54% | 0.1939 |
| RAG P2 | 82/83 | 2 | 10.54% | 0.1939 |
| RAG B1 | 67/83 | 33 | 11.01% | 0.2267 |

각 Direct는 모델을 13회 호출했습니다. 모든 군에서 HL7 실행은 완료됐고
평가 환자 Bundle은 검증에 실패했습니다. P1·P2는 Encounter가 누락됐습니다.
보정으로 HL7 오류는 감소했고 독립 참조 점수는 각 정책 안에서 같았습니다.
평가 환자가 1명이므로 RAG 대응 결과의 신뢰구간은 산출되지 않았습니다.
결과는 GPU 경로의 `no-rag-results/manifest.json`,
`rag-results/manifest.json`, `rag-ablation.json`에 있습니다.

Synthea GPU 준비는 177.3초에 완료했고 4종 Profile을 생성했습니다.
Observation 보정에서 준비 오류 194건을 해결했으며, 전체 준비 검증을 통과한
Profile은 0종입니다. 평가 환자 2명의 세 비교군 결과는 다음과 같습니다.

| Synthea GPU | 생성 / 원천 행 | HL7 오류 | 참조 필드 정확도 | 이벤트 F1 |
| --- | ---: | ---: | ---: | ---: |
| P1 | 252/322 | 263 | 56.39% | 0.6754 |
| P2 | 252/322 | 50 | 63.36% | 0.6754 |
| B1 | 293/322 | 503 | 67.52% | 0.6440 |

Direct는 GPU 모델을 42회 호출했고 평가에 727.6초가 걸렸습니다.
세 군 모두 HL7 실행 완료, fatal 0건, 환자 Bundle 통과 0/2였습니다.
P1·P2는 문자 Observation 70건을 숫자 변환에서 누락했습니다. 두
Profile이 Patient와 Encounter ID에 CSV 행 식별자를 사용하여 참조 키가
업무 키와 불일치했습니다. P2는 참조 무결성 검사의 미해결 참조 437건을
기록했습니다. B1은 Encounter 5건과 Observation 24건이 부족했습니다.
결과는 GPU 경로의 `synthea-results/manifest.json`에 있습니다.

## 연구 실험의 잔여 범위

두 데이터셋의 소규모 검증으로 실행 경로와 지표 출력을 확인합니다. 본실험은
사전에 고정한 비교 범위와 충분한 환자 수로 수행합니다. Synthea 패널 및 MIMIC
시간대 차이는 본실험의 참조 계약에서 별도로 정의해야 합니다. pilot의
원천 assertion은 수동 정의한 계약으로, 논문에는 적용 범위와 개입량을 기록합니다.

이전 코호트를 역할별로 고정해 다시 추출하고 실행한 11월 21일 개발 파일럿은
[고정 환자 검증 보고서](./fixed_pilot_report.md)에 정리했습니다. 새 평가 기준과
기존 결과의 점수 분모가 다르므로 두 날짜의 수치를 같은 지표로 바로 비교하지 않습니다.
