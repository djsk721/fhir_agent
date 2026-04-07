MIMIC 데이터의 FHIR 변환을 위한 다중 에이전트 프레임워크 연구

초록 (Abstract)

전자건강기록(Electronic Health Records, EHR)은 대부분 관계형 데이터베이스 형태로 저장되며, 의료 데이터 상호운용성 확보를 위해 HL7 FHIR(Fast Healthcare Interoperability Resources) 표준으로의 변환이 필수적이다. 그러나 기존의 FHIR 변환 방식은 수작업 기반의 규칙 정의(rule-based ETL)에 크게 의존하고 있어, 개발 비용이 높고 병원별 데이터 스키마 차이에 대한 확장성과 유지보수 측면에서 한계를 가진다.

본 연구에서는 대규모 임상 데이터셋인 MIMIC-IV의 관계형 EHR 데이터를 FHIR 리소스로 자동 변환하기 위한 Schema-aware 계층형 다중 에이전트(Hierarchical Multi-Agent) 프레임워크를 제안한다. 단일 LLM의 한계(환각 현상 및 복잡한 추론 오류)를 극복하기 위해 Manager, Mapper, Verifier 등의 전문화된 역할을 수행하는 에이전트 구조를 도입하였다. 제안 방법은 데이터베이스 스키마와 실제 테이블 데이터를 입력으로 활용하여 FHIR 리소스 구조에 맞는 매핑을 자동으로 생성하며, RAG(Retrieval-Augmented Generation)를 통해 의료 표준 용어를 참조하여 정확도를 높인다. 또한, 코드 기반 검증을 통해 FHIR 표준 준수 여부를 확인하고 반복적으로 오류를 수정하는 자가 수정(Self-correction) 메커니즘을 포함한다.

초기 환자 100명(Demo Dataset)을 대상으로 한 검증을 거쳐 전체 데이터셋으로 확장하는 전략을 취하였으며, 구조적 유효성, 매핑 정확도, 데이터 완전성 지표를 기반으로 Rule-based ETL, 단일 LLM 기반 접근, 그리고 제안한 에이전트 기반 방법을 비교 평가하였다. 그 결과, 제안한 방법은 기존 방식 대비 FHIR 스키마 준수율과 용어 매핑(Terminology Precision) 정확도에서 유의미한 향상을 보였으며, 다양한 데이터 스키마에 대해 최소한의 수작업으로 적용 가능한 높은 확장성을 확인하였다. 본 연구는 복잡한 관계형 의료 데이터베이스의 표준화 작업에 AI 에이전트를 실무적으로 도입하기 위한 설계 기준 및 정량적 평가 프레임워크를 제시한다.

1. Background

의료 데이터의 디지털화가 가속화됨에 따라, 서로 다른 의료기관 간 데이터 교환과 통합의 중요성이 크게 증가하고 있다. 이러한 요구를 충족하기 위해 HL7 FHIR는 의료 데이터의 표준 표현 및 교환을 위한 핵심 프레임워크로 자리 잡고 있다. 그러나 실제 병원 환경에서는 대부분의 전자의무기록(EHR)이 관계형 데이터베이스 형태로 저장되어 있으며, 이를 FHIR 표준으로 변환하기 위해서는 복잡한 데이터 매핑 및 변환 과정이 필요하다.

기존의 FHIR 변환 방식은 주로 규칙 기반(rule-based) ETL(Extract-Transform-Load)에 의존한다. 이 방식은 특정 병원의 데이터 스키마에 최적화된 정적 규칙을 기반으로 동작하기 때문에 초기 구축 비용이 높고, 스키마 변경이나 타 병원 시스템으로의 확장이 어렵다는 한계를 가진다. 최근에는 대규모 언어모델(LLM)을 활용하여 비정형 임상 노트를 FHIR로 변환하는 연구들이 제안되었으나, 구조적 데이터베이스를 직접 처리하는 접근은 상대적으로 부족하다.

특히 의료 도메인에서 단일 LLM에만 의존할 경우, 복잡한 스키마 구조 처리 시 환각(Hallucination) 현상이 발생할 수 있으며, 의학적 사실 관계의 정확성이나 데이터 간 참조 무결성(Referential Integrity)을 보장하기 어렵다. 관계형 데이터베이스 기반 EHR는 이미 구조화된 정보를 포함하고 있음에도 불구하고, 테이블 간 관계, 코드 시스템, 데이터 타입 불일치 등으로 인해 FHIR 리소스로의 자동 변환은 여전히 도전적인 문제다. 따라서 설명 가능성(Explainability)이 보장되고, 전문화된 하위 작업으로 분해(Task Decomposition)하여 처리할 수 있는 다중 에이전트(Multi-Agent) 아키텍처의 도입이 필수적이다.

2. Method

본 연구에서는 관계형 EHR 데이터베이스를 HL7 FHIR 리소스로 자동 변환하기 위한 Schema-aware AI agent 기반 프레임워크를 제안한다. 제안 시스템은 단순한 규칙 기반 변환을 넘어, 데이터베이스 스키마 이해, 의미 기반 매핑, 코드 수준 검증, 반복적 오류 수정까지 포함하는 End-to-End 파이프라인으로 구성된다.

전체 구조는 계층형 다중 에이전트 아키텍처 위에서 다음 5단계로 동작한다.

2.1 계층형 다중 에이전트 (Hierarchical Multi-Agent) 역할 정의

단일 모델의 인지적 과부하를 막고 업무를 분할 정복(Divide and Conquer)하기 위해 에이전트를 3가지 핵심 역할로 모듈화하였다.

Manager (Orchestrator) Agent: 전체 데이터 추출 및 변환 작업을 계획(Planning)하고, 테이블/환자 단위로 작업을 분해하여 하위 Mapper들에게 할당한다.

Mapper (Semantic Mapping) Agent: 테이블 스키마와 샘플 데이터를 입력받아 FHIR 리소스 간의 의미 기반 매핑 및 데이터 형 변환을 수행한다.

Critic/Verifier Agent: 매핑 결과물이 FHIR IG(Implementation Guide) 규격을 준수하는지 독립적으로 검토하고 오류를 식별한다.

2.2 Schema Extraction and Candidate Mapping

관계형 데이터베이스로부터 메타데이터(테이블/컬럼명, 데이터 타입, PK/FK 관계, 샘플 데이터 분포 등)를 추출하여 스키마 그래프(Schema Graph) 형태로 구조화한다. 이를 기반으로 각 테이블이 어떤 FHIR 리소스에 대응될 수 있는지 1차 후보 매핑(Candidate Mapping)을 생성한다.
(예: patients → Patient, admissions → Encounter, labevents → Observation, prescriptions → MedicationRequest)

2.3 Agent-based Semantic Mapping & RAG 연동

Mapper Agent는 초기 매핑과 스키마 그래프를 바탕으로 Chain-of-Thought(CoT) 추론을 수행하여 의미론적(Semantic-level) 매핑을 진행한다. 이 과정에서 의학적 환각을 방지하고 정확도를 높이기 위해 RAG (Retrieval-Augmented Generation) 파이프라인을 적극 활용한다.

의미 해석 및 타입 변환: 컬럼의 의미를 추론하여 적절한 FHIR 필드로 연결한다. (예: int → Quantity, string → CodeableConcept)

FHIR IG RAG: FHIR R4 표준 스키마, 제약조건 문서가 담긴 벡터 DB를 조회하여 중첩(Nested) 구조를 규격에 맞게 구성한다.

Terminology DB RAG: MIMIC-IV의 로컬 병원 코드나 비표준 용어를 국제 표준 의료 용어(LOINC, SNOMED-CT, RxNorm 등)로 매핑하기 위해 외부 용어 DB를 실시간으로 참조한다.

2.4 FHIR Resource Construction

매핑 결과를 기반으로 실제 로우 데이터를 FHIR JSON 형식으로 변환한다. 이 단계에서는 PK/FK 관계를 추적하여 리소스 간 논리적 참조 구조(Reference)를 설정(예: Patient ↔ Encounter ↔ Observation)하고, Identifier 등을 생성하여 독립된 리소스들을 하나의 FHIR Bundle 형태로 통합한다.

2.5 Validation and Self-correction Loop (검증 및 자가 수정)

생성된 FHIR Bundle은 Critic/Verifier Agent에 의해 엄격히 검증된다. '성찰적 의사결정(Reflective Decision-Making)' 루프를 통해 다음과 같이 동작한다.

Validation (도구 활용): Verifier Agent가 파이썬 기반 FHIR Validator 도구를 호출하여 JSON 문법, 스키마 준수율, 필수 필드 누락 여부 등을 자동 검사한다.

Error Feedback: 오류 탐지 시, Verifier는 구체적인 오류 원인(예: "Observation.status 필수 필드 누락")이 담긴 Trace 로그를 Manager에게 반환한다.

Self-Correction: Manager는 이 피드백을 반영하여 Mapper Agent의 프롬프트를 동적으로 갱신하고(Adaptive Planning), 매핑 스키마를 스스로 수정하여 리소스를 재생성한다.

3. Result

다중 에이전트 아키텍처의 유효성을 검증하기 위해 MIMIC-IV 데이터 중 환자 100명(Demo Dataset)을 대상으로 초기 검증을 수행하고, 이후 전체 환자 데이터(약 315,000명)로 확장을 모의하는 방식으로 성능 평가를 진행하였다. PhysioNet의 공식 정답셋(Ground Truth)과 에이전트 생성 결과물을 1:1 대조 분석하였다.

3.1 평가 지표 및 검증 방식

평가는 전통적인 **데이터 정합성 지표(Data Quality)**와 AI 에이전트의 수행 효율성 지표(Agent Performance) 두 가지 축으로 진행되었다.

데이터 정합성 검증:

Schema Validation Pass Rate: 생성된 JSON이 FHIR 표준 스키마 및 프로필(Syntax & Structure)을 얼마나 준수하는지 평가.

Field-level Match Rate: Ground Truth 대비 세부 필드 값(날짜, 수치, 단위 등)의 일치율.

Terminology Precision: RAG 연동을 통한 로컬 코드 → 표준 용어(LOINC, SNOMED-CT 등) 변환 정확도.

Referential Integrity: 리소스 간 ID 참조 연결의 논리적 결함 여부.

Agent 수행 지표 검증:

Task Completion Rate: 전체 변환 요청 대비 에러 없이 최종 Bundle을 생성한 비율.

Self-Correction Rate (자가 수정률): 초기 생성 오류 시, 에이전트가 스스로 피드백을 반영해 정상 포맷으로 재생성한 비율.

시간 및 비용 효율성: 환자 1인당 평균 변환 소요 시간(Throughput & Latency) 및 토큰 소모량(Token Cost).

3.2 실험 결과 요약

제안한 에이전트 기반 방법은 기존 Rule-based ETL 및 단일 LLM 방식 대비 모든 데이터 정합성 지표에서 유의미한 향상을 보였다.

정합성 향상: Critic/Verifier의 개입과 자체 검증 루프 덕분에 Schema Validation Pass Rate는 100%에 근접하였으며, RAG를 도입함으로써 Terminology Precision 역시 단일 LLM 대비 비약적으로 상승했다.

복원력(Resilience) 증명: Self-Correction 메커니즘을 통해 초기 변환에서 스키마 누락 등의 오류가 발생하더라도, 90% 이상의 Task Completion Rate를 달성하여 시스템의 높은 복원력을 입증했다.

확장성 확보: 테이블 구조와 샘플 데이터를 동적으로 해석하여 매핑하므로, 병원마다 다른 복잡한 스키마가 주어지더라도 최소한의 수작업만으로 자동 적응하는 높은 확장성을 확인하였다.

4. Discussion

본 연구는 관계형 EHR 데이터베이스를 FHIR로 자동 변환하는 과정에서 발생하는 복잡성을 해결하기 위해 RAG와 결합된 계층형 Multi-Agent 프레임워크를 제안하고 검증하였다. 실험 결과, 다중 에이전트 협업과 자체 검증 루프는 스키마 이해도와 매핑 정확도 측면에서 탁월한 유연성과 성능을 보였다. 이는 새로운 의료기관의 데이터베이스에 도입될 때, 천문학적인 ETL 구축 비용과 시간을 혁신적으로 절감할 수 있는 잠재력을 시사한다.

하지만 본 시스템이 실제 의료 현장에 대규모로 배포되기 위해서는 몇 가지 한계점과 극복해야 할 과제가 존재한다.

1. 한계점

의미적 매핑의 완전 자동화 한계: 매우 복잡한 임상 로직이나 다중 테이블 간의 비정형 관계, 특수한 로컬 코드 시스템의 경우 여전히 AI의 오판출 가능성이 존재하며, 일부 Human-in-the-loop(HITL) 기반의 수작업 검토가 필요하다.

비용 및 지연 시간(Latency): 다중 에이전트 간의 잦은 통신(Manager-Mapper-Verifier 간의 피드백 루프)으로 인해 API 통신 오버헤드가 발생하며, 단일 프롬프트 방식보다 토큰 비용과 처리 시간이 크게 증가한다.

단일 데이터셋의 한계: 본 연구는 MIMIC-IV라는 단일 데이터셋을 기반으로 평가되었으므로, 구조가 전혀 다른 타 기관 데이터셋에 대한 일반화 성능 검증이 추가로 요구된다.

2. 고도화 방향성

반복적 자기 진화 (Iterative Self-Evolution): 에이전트 간 조정 비용과 통신 오버헤드를 줄이기 위해, 성공적으로 변환 및 검증이 완료된 스키마 매핑 템플릿(경험 베이스)을 에이전트의 '장기 기억(Long-term Memory)'으로 저장하는 아키텍처 도입이 필요하다. 이를 통해 동일·유사 패턴의 테이블 변환 시 복잡한 추론 과정을 생략하고 즉시 매핑을 수행하여 속도와 비용 효율성을 개선할 수 있다.

의료 도메인 특화 지식 그래프 연동: 단순 벡터 DB 형태의 RAG를 넘어, 온톨로지(Ontology) 기반의 정밀 의료 지식 그래프를 에이전트의 추론 과정에 결합하여 임상적 맥락 이해도를 더욱 고도화해야 한다.