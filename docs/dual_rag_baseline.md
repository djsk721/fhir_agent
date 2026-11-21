# 원천 스키마와 FHIR 이중 RAG 베이스라인

MIMIC에서 개발한 변환 방식을 잠근 뒤 Synthea에 적용한다. 대상 원천 테이블과 FHIR 리소스의 범위는 읽기 전용 추출 명세로 정하고, 개별 필드의 변환 규칙은 LLM이 생성한다. 원천 스키마와 FHIR 문서는 서로 다른 벡터 인덱스에 보관한다. 평가 정답과 환자 행은 두 인덱스에 넣지 않는다.

## 비교 설계

| 실험군 | 초기 Profile 생성에 제공하는 근거 | 보정 |
| --- | --- | --- |
| FHIR-only | 원천의 허용 컬럼·설명·PK/FK, 선언된 FK 이웃, 공식 R4 제약, FHIR 벡터 검색 문서 | 준비 환자에서 실행한 결과와 Validator·원천 값 검사 피드백 |
| 이중 RAG | FHIR-only와 동일한 근거에 원천 스키마 벡터 검색 문서만 추가 | 동일한 피드백 절차와 한도 |

P1은 초기 Profile, P2는 보정 Profile을 고정 평가 환자에서 실행한다. 두 방식의 초기 생성에는 예시 행을 제공하지 않는다(`sample_rows=0`). 초기 생성에 형식 검증 재시도는 있을 수 있지만, 이 단계에서는 환자 레코드로 Profile을 검사하거나 수정하지 않는다. Self-Correction은 이후 준비 환자에서만 수행한다. FHIR와 매핑의 독립 참조는 평가 단계에서만 사용한다.

### 인덱스와 정확 조회

- FHIR 검색은 `db/fhir.schema.json`에서 만든 기존 FHIR 전용 인덱스를 공유한다. 벡터 검색 순위와 별도로, 동일 공식 R4 `StructureDefinition` 스냅샷에서 요소 경로, 카디널리티, 선택 타입, 참조 대상과 용어 바인딩을 조회한다. 패키지 내용 해시를 두 정책에 기록한다.
- 원천 인덱스는 코호트의 추출 명세와 같은 해시를 가져야 한다. 검토된 테이블·컬럼·키 정보를 문서화하고, 대상 테이블과 선언된 FK 이웃 안에서 벡터 유사도와 이름 일치를 이용해 검색한다. 이웃을 확인하는 정확 조회는 두 방식에 공통이다. 이번 원천 인덱스는 승인된 스키마 범위를 사용하며 전체 DB의 자동 탐색은 수행하지 않는다. 명세에 없는 컬럼 타입·널 허용·코드 체계는 추정해 적재하지 않는다.
- MIMIC과 Synthea는 각 방식의 정책 잠금을 재사용한다. FHIR 인덱스와 모델·프롬프트·검색 방법·보정 한도는 그대로 두고, 원천 인덱스만 데이터셋에 맞춰 바꾼다. Profile 생성과 평가에는 동일한 데이터셋별 원천 인덱스를 사용해야 한다. 이 방식은 로컬 인덱스와 FK 탐색으로 구현하며 별도 그래프 DB는 필요하지 않다.

### 평가 범위

MIMIC Demo 준비 1명·평가 1명, Synthea 준비 1명·평가 2명의 기존 고정 코호트를 재사용한다. 환자와 원천 행, 독립 FHIR 참조, 부분 매핑 참조의 해시를 먼저 확인한다. 이 실험은 개발 파일럿이며 새로운 환자 검증이나 실제 병원 간 일반화의 근거가 아니다. 환자별 대응 분석은 입력과 참조가 같고 두 정책에서 `rag.source_enabled`만 다를 때 실행한다.

## 재실행

기반 설정과 기존 FHIR 인덱스, 로컬 공식 R4 패키지를 준비한다. 패키지 내용이나 코드가 달라지면 새 정책으로 잠근다. 아래의 환경 변수는 사용할 실제 파일을 가리키며, 출력에는 새 디렉터리를 지정한다.

```bash
python main.py research baseline-configs --config "$BASE_CONFIG" \
  --fhir-constraints "$FHIR_CORE_PACKAGE" --output-dir "$RUN_DIR"
for arm in fhir-only dual; do
  python main.py research freeze --config "$RUN_DIR/$arm.config.json" \
    --index "$FHIR_INDEX" --output "$RUN_DIR/$arm.lock.json"
done

bash experiments/run_dual_rag_baseline.sh \
  --fhir-only-lock "$RUN_DIR/fhir-only.lock.json" --dual-lock "$RUN_DIR/dual.lock.json" \
  --dual-config "$RUN_DIR/dual.config.json" --index "$FHIR_INDEX" \
  --source-spec "$SOURCE_SPEC" --preparation "$PREPARED" --test "$TEST_RECORDS" \
  --cohort-lock "$COHORT_LOCK" --reference "$FHIR_REFERENCE" \
  --reference-profiles "$MAPPING_REFERENCE" --output "$NEW_DATASET_RUN"
```

MIMIC의 `SOURCE_SPEC`은 해당 코호트를 준비한 MIMIC 추출 명세다. Synthea의 `SOURCE_SPEC`은 생성기 정보가 포함된 실제 스냅샷의 명세다. 범용 CSV 명세의 이름이 같아도 내용 해시가 다르면 실행하지 않는다. 실행 기록과 검증 오류에는 리소스 식별자가 포함될 수 있으므로 원천 자료와 함께 로컬 산출물 폴더에 둔다. 코호트 고정 및 평가 필드 정의는 [연구 프로토콜](./research_protocol.md)에 있다.

## 고정 코호트 검증 결과

두 방식 모두 로컬 Ollama의 `gpt-oss:20b`와 `nomic-embed-text`를 사용했고, 실행 중 두 모델이 GPU에 배치된 것을 확인했다. HL7 Java Validator는 FHIR R4 4.0.1의 로컬 패키지를 사용했다. 원격 용어 서버는 비활성화했다. 새 배선까지 포함한 코드 테스트 156건이 통과했다.

### MIMIC Demo

| 지표 | FHIR-only P1 / P2 | 이중 RAG P1 / P2 |
| --- | ---: | ---: |
| 생성 / 원천 행 | 68/83 · 68/83 | 68/83 · 68/83 |
| HL7 error / fatal | 0/0 · 0/0 | 0/0 · 0/0 |
| 생성 Bundle 검증 통과 | 1/1 · 1/1 | 1/1 · 1/1 |
| 종합 환자 통과 | 0/1 · 0/1 | 0/1 · 0/1 |
| 독립 참조 필드 정확도 | 0.47% · 0.47% | 0.23% · 0.23% |
| 독립 매핑의 필드 대응 일치 | 15/20 · 15/20 | 14/20 · 14/20 |
| 독립 참조 이벤트 매칭 | 2/83 · 2/83 | 2/83 · 2/83 |

두 방식 모두 Condition의 코드 매핑이 실행 명세 형식 검증에서 거부되어 Condition 15행이 누락됐다. P2는 생성된 Profile을 보정하므로, 초기 생성이 실패한 리소스까지 새로 만들지는 않는다. 이중 RAG는 보정 후보를 시도했으나 최종적으로 채택한 보정으로 결과가 개선되지는 않았다.

Bundle의 형식 검사는 제출된 리소스를 검사하므로 원천 Condition이 빠져 있어도 통과할 수 있다. 종합 평가는 예상 원천 행 수와 독립 참조 충실도도 검사하므로 두 군 모두 불합격이다. 두 군의 Observation 66건이 모두 `Z` 오프셋으로 생성됐다. 기존 고정 코호트 [시각 진단](./fixed_pilot_report.md#변경-내용과-분모)에서 확인한 참조와의 시간대 차이를 이번 점수에 소급 보정하지 않았다. MIMIC 평가 환자는 한 명으로, 원천 검색 효과의 신뢰구간을 산출하지 않았다.

### Synthea

| 지표 | FHIR-only P1 / P2 | 이중 RAG P1 / P2 |
| --- | ---: | ---: |
| 생성 / 원천 행 | 23/322 · 23/322 | 259/322 · 259/322 |
| HL7 error / fatal | 2/0 · 2/0 | 494/0 · 494/0 |
| 생성 Bundle 검증 통과 | 0/2 · 0/2 | 0/2 · 0/2 |
| 종합 환자 통과 | 0/2 · 0/2 | 0/2 · 0/2 |
| 독립 참조 필드 정확도 | 1.87% · 1.87% | 88.38% · 88.38% |
| 독립 매핑의 필드 대응 일치 | 8/28 · 8/28 | 24/28 · 24/28 |
| 독립 참조 이벤트 매칭 | 23/280 · 23/280 | 259/280 · 259/280 |

FHIR-only는 Patient와 Observation의 초기 Profile 생성에 실패했다. 이중 RAG는 두 Profile을 생성했지만 존재하지 않는 `Encounter.class.coding.0`을 제안해 Encounter Profile이 거부됐다. 따라서 입원 21행이 빠지고, 그 입원을 가리키는 참조가 해소되지 않았다. Observation은 입원 키를 리소스 ID의 필수 구성요소로 선택했다. 저장된 P2 Profile을 Renderer로 재검사해, 입원 키가 비어 있는 42행이 모두 이 ID 규칙으로 누락됐음을 확인했다.

이중 RAG가 Synthea에서 더 많은 리소스를 생성하고 독립 참조와 더 많이 일치시켰지만 표준 검증을 통과시킨 것은 아니다. HL7 오류 2건과 494건은 검사된 리소스의 수와 종류가 크게 다른 결과의 집계다. 이 원시 오류 수로 변환 품질을 단독 비교하지 않는다. 표의 필드 정확도는 두 환자의 참조 필드를 합친 점수이며, 대응 산출물의 환자별 평균 차이와 분모가 다르다.

### 결론과 산출물

원천 검색을 추가한 효과는 데이터셋에 따라 달랐다. MIMIC에서는 완전성이 그대로였고, Synthea에서는 누락이 줄었지만 전체 결과의 표준 적합성이 확보되지 않았다. 이번 고정 파일럿에서 네 실험의 P1·P2 생성 행 수와 독립 평가 점수는 같았다. 준비 단계에서 오류를 모두 해결한 Profile은 없었다.

다음 비교 전에 **초기 Profile의 경로·연산을 더 엄격하게 제한**하고, 필수가 아닌 원천 FK가 비어 있어도 리소스 ID를 만들 수 있는지 확인한다. MIMIC은 ICD 버전별 코딩 규칙과 시간대 기준을, Synthea는 Encounter의 Coding과 Observation의 선택적 입원 연결을 우선 점검한다. 이를 바탕으로 새 환자 코호트를 고정한 뒤 원천 RAG 및 보정 효과를 다시 추정한다.

실제 원천 인덱스는 MIMIC 25개, Synthea 31개 문서였다. `artifacts/dual-rag-baseline-20261121/`의 `mimic/`과 `synthea/`에 각각 `fhir-only/manifest.json`, `dual/manifest.json`, `source-rag-comparison.json`이 있다. 두 데이터셋의 같은 실험군은 동일 정책 해시를 공유하며, 이중 RAG의 원천 인덱스 해시는 데이터셋별로 다르다. 이 파일럿의 부분 매핑 참조와 소수 환자 결과는 확정된 전문가 정답이나 일반화의 근거가 아니다.
