import ast
import re
from typing import Dict, Any, List
from sqlalchemy import create_engine, inspect
from langchain_community.utilities import SQLDatabase

from config import config

# ==========================================
# 1. DB Connector & Tool Gateway (Sandbox 적용)
# ==========================================
class DatabaseGateway:
    """에이전트의 무분별한 DB 탐색을 제어하는 Sandbox Gateway"""
    
    def __init__(self, uri: str, schemas: List[str], allowed_tables: List[str]):
        self.engine = create_engine(uri, connect_args={"options": f"-c search_path={','.join(schemas)}"})
        self.schemas = schemas
        
        # 소문자로 정규화하여 저장 (대소문자 구분 없이 검증하기 위함)
        self.allowed_tables = [table.lower() for table in allowed_tables]
        
        # 1차 방어: LangChain 에이전트가 스키마를 조회할 때 허용된 테이블만 보이도록 격리
        self.db = SQLDatabase(self.engine, include_tables=self.allowed_tables)

    def _extract_tables_from_query(self, query: str) -> List[str]:
        """쿼리 문자열에서 단순 정규식을 이용해 테이블 명을 추출하는 내부 메서드"""
        # FROM 또는 JOIN 다음에 오는 단어(테이블명)를 추출
        pattern = re.compile(r'\b(?:FROM|JOIN)\s+([a-zA-Z0-9_]+)\b', re.IGNORECASE)
        matches = pattern.findall(query)
        return [match.lower() for match in matches]

    def execute_safe_query(self, query: str) -> List[Any]:
        """Mapper 에이전트가 호출하는 도구 (샌드박스 검증 포함)"""
        
        # [Sandbox 방어 1] DML/DDL 원천 차단
        if not query.strip().upper().startswith("SELECT"):
            raise ValueError("[Security Block] Tool Gateway는 SELECT 쿼리만 허용합니다.")
        
        # [Sandbox 방어 2] 타겟 테이블 제한 (Whitelist 검증)
        queried_tables = self._extract_tables_from_query(query)
        
        if not queried_tables:
            raise ValueError("[Security Block] 쿼리에서 대상 테이블을 식별할 수 없습니다.")
            
        for table in queried_tables:
            # 쿼리에 포함된 테이블이 허용 목록(allowed_tables)에 없으면 즉시 차단
            if table not in self.allowed_tables:
                raise ValueError(
                    f"[Security Block] 접근 거부: '{table}' 테이블은 탐색이 허용되지 않았습니다. "
                    f"허용된 테이블: {self.allowed_tables}"
                )

        # 검증 통과 시 쿼리 실행
        try:
            raw_result = self.db.run(query)
            return ast.literal_eval(raw_result)
        except Exception as e:
            return [{"error": f"쿼리 실행 실패: {str(e)}"}]

    def get_schema_metadata(self) -> Dict[str, Any]:
        """허용된 테이블(allowed_tables)에 대해서만 스키마 추출"""
        inspector = inspect(self.engine)
        metadata = {}
        
        for schema in self.schemas:
            metadata[schema] = {}
            for table in self.allowed_tables:
                # 해당 스키마에 테이블이 존재하는지 확인 후 메타데이터 추출
                if inspector.has_table(table, schema=schema):
                    columns = inspector.get_columns(table, schema=schema)
                    metadata[schema][table] = [
                        {"column_name": col["name"], "data_type": str(col["type"])}
                        for col in columns
                    ]
        return metadata

DB_URI = f"postgresql+psycopg2://{config['TEST_USER']}:{config['TEST_PW']}@localhost:5432/{config['TEST_DB']}"

# 에이전트가 탐색 가능한 타겟 테이블을 엄격하게 제한 (Sandbox)
TARGET_TABLES = ["patients", "admissions", "diagnoses_icd"]

gateway = DatabaseGateway(
    uri=DB_URI, 
    schemas=["mimiciv_hosp"], 
    allowed_tables=TARGET_TABLES
)

# 테스트 1: 허용된 테이블 쿼리 -> 정상 실행
print("Test 1:", gateway.execute_safe_query("SELECT subject_id FROM patients LIMIT 5"))

# 테스트 2: 허용되지 않은 테이블 쿼리 시도 -> Security Block 발생!
try:
    gateway.execute_safe_query("SELECT * FROM d_labitems LIMIT 5")
except ValueError as e:
    print("Test 2 (차단 성공):", e)

import os
import json
from typing import List
from langchain_core.documents import Document
from langchain_community.vectorstores import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

# ==========================================
# Phase 2: FHIR JSON Schema Vector DB Builder
# ==========================================
class FHIRSchemaKnowledgeBuilder:
    """단일 fhir.schema.json 파일을 파싱하여 RAG용 Vector DB를 구축하는 클래스"""
    
    def __init__(self, persist_directory: str = "./fhir_schema_vectordb"):
        self.persist_directory = persist_directory
        self.embedding_model = HuggingFaceEmbeddings(
            model_name="sentence-transformers/all-MiniLM-L6-v2"
        )
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=800, 
            chunk_overlap=100
        )

    def _parse_fhir_schema(self, json_data: dict) -> List[Document]:
        """JSON Schema 내의 'definitions'와 'properties'를 평탄화하여 문서로 추출"""
        docs = []
        
        # 최상위 definitions 추출 (Patient, Observation, Quantity 등)
        definitions = json_data.get("definitions", {})
        
        for def_name, def_data in definitions.items():
            # 1. 리소스 / 데이터 타입 개요 (Overview) 추출
            desc = def_data.get("description", "No description available.")
            overview_text = f"Resource/DataType: {def_name}\nDescription: {desc}"
            
            docs.append(Document(
                page_content=overview_text,
                metadata={"source": "fhir.schema.json", "category": "overview", "definition": def_name}
            ))
            
            # 2. 세부 속성 (Properties) 추출
            properties = def_data.get("properties", {})
            for prop_name, prop_data in properties.items():
                prop_desc = prop_data.get("description", "")
                
                # 타입 정보 조립 (일반 타입, 참조($ref), 배열(items) 처리)
                type_info = prop_data.get("type", "")
                if "$ref" in prop_data:
                    # "#/definitions/string" -> "string" 형태로 추출
                    ref_name = prop_data["$ref"].split("/")[-1]
                    type_info += f" (Reference to {ref_name})"
                elif "items" in prop_data and "$ref" in prop_data["items"]:
                    ref_name = prop_data["items"]["$ref"].split("/")[-1]
                    type_info += f" Array of {ref_name}"
                
                # 열거형(Enum) 및 정규식(Pattern) 규칙 추출
                enum_info = f"\nAllowed Values (Enum): {', '.join(prop_data['enum'])}" if "enum" in prop_data else ""
                pattern_info = f"\nValidation Pattern: {prop_data['pattern']}" if "pattern" in prop_data else ""

                # 에이전트가 이해하기 쉽도록 평문 구성
                prop_text = (
                    f"Resource/DataType: {def_name}\n"
                    f"Property Name: {prop_name}\n"
                    f"Data Type: {type_info.strip()}\n"
                    f"Description: {prop_desc}"
                    f"{enum_info}{pattern_info}"
                )
                
                docs.append(Document(
                    page_content=prop_text,
                    metadata={"source": "fhir.schema.json", "category": "property", "definition": def_name, "property": prop_name}
                ))
                
        return docs

    def build_vector_db(self, schema_file_path: str):
        """지정된 schema 파일을 읽어 Chroma DB로 구축"""
        print(f"[🚀] '{schema_file_path}' 파싱을 시작합니다...")
        
        if not os.path.exists(schema_file_path):
            print(f"[!] 파일을 찾을 수 없습니다: {schema_file_path}")
            return

        # 1. 파일 로드 및 파싱
        with open(schema_file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        all_documents = self._parse_fhir_schema(data)
        
        if not all_documents:
            print("[!] 추출된 문서가 없습니다. JSON 구조를 확인하세요.")
            return

        print(f"[⏳] 총 {len(all_documents)}개의 속성(Property) 기반 문서가 추출되었습니다.")
        
        # 2. 청킹 (긴 Description으로 인한 컨텍스트 초과 방지)
        split_docs = self.text_splitter.split_documents(all_documents)
        print(f"[⏳] 청킹 완료: {len(split_docs)}개의 청크(Chunk) 임베딩 진행 중...")
        
        # 3. Vector DB 저장
        self.vector_db = Chroma.from_documents(
            documents=split_docs,
            embedding=self.embedding_model,
            persist_directory=self.persist_directory
        )
        
        print(f"[✅] FHIR Schema Vector DB 구축 완료! (저장 폴더: {self.persist_directory})")

# ==========================================
# 실행 테스트 (Execution)
# ==========================================
if __name__ == "__main__":
    # 코드가 있는 경로에 업로드하신 'fhir.schema.json' 파일이 있다고 가정합니다.
    builder = FHIRSchemaKnowledgeBuilder(persist_directory="./fhir_schema_vectordb")
    
    # 실제 fhir.schema.json 파일 경로 입력
    builder.build_vector_db("./db/fhir.schema.json")

from typing import List, Tuple
from langchain_community.vectorstores import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_core.documents import Document


class MedicalKnowledgeRetriever:
    """구축된 Chroma Vector DB를 로드하고 검색을 수행하는 클래스"""
    
    def __init__(self, persist_directory: str = "./mimic_docs_vectordb"):
        # DB 생성 시 사용했던 것과 완벽히 동일한 임베딩 모델을 로드해야 합니다.
        self.embedding_model = HuggingFaceEmbeddings(
            model_name="sentence-transformers/all-MiniLM-L6-v2"
        )
        
        # 로컬 디스크에 저장된 Chroma DB 로드
        self.vector_db = Chroma(
            persist_directory=persist_directory,
            embedding_function=self.embedding_model
        )
        print(f"[✅] Vector DB 로드 완료: {persist_directory}")

    def search_similar_items(self, query: str, k: int = 5) -> List[Document]:
        """주어진 쿼리와 유사한 문서를 반환 (기본 검색)"""
        print(f"\n검색 쿼리: '{query}' (Top-{k})")
        results = self.vector_db.similarity_search(query, k=k)
        return results

    def search_with_scores(self, query: str, k: int = 5) -> List[Tuple[Document, float]]:
        """유사도 점수(Score)와 함께 문서를 반환 (L2 Distance 기반, 낮을수록 유사함)"""
        print(f"\n검색 쿼리(점수 포함): '{query}' (Top-{k})")
        # L2 거리(Distance)를 반환하므로 점수가 낮을수록 유사도가 높음
        results_with_scores = self.vector_db.similarity_search_with_score(query, k=k)
        return results_with_scores


# Retriever 인스턴스 생성
retriever = MedicalKnowledgeRetriever()

# 단순 유사도 검색
query_1 = "blood pressure"
docs = retriever.search_similar_items(query_1, k=3)

print("-" * 40)
for i, doc in enumerate(docs, 1):
    print(f"[{i}] 출처(Source): {doc.metadata.get('source')}")
    print(f"    내용: {doc.page_content}")

#  테스트 쿼리 2: 점수 포함 검색 (특정 질병명 검색)
query_2 = "diabetes mellitus"
scored_docs = retriever.search_with_scores(query_2, k=3)

print("-" * 40)
for i, (doc, score) in enumerate(scored_docs, 1):
    print(f"[{i}] 거리(Distance): {score:.4f} | 출처: {doc.metadata.get('source')}")
    print(f"    내용: {doc.page_content}")


import json
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from langchain_ollama import ChatOllama

# ==========================================
# 1. Pydantic 스키마 정의 (CoT 및 구조화된 출력 강제)
# ==========================================

class FHIRMapperOutput(BaseModel):
    """Mapper 에이전트의 출력 스키마. CoT를 위한 thought_process를 강제합니다."""
    thought_process: str = Field(
        description="Chain of Thought reasoning for mapping. "
                    "Explain which MIMIC column maps to which FHIR field, data type conversions, "
                    "and any RAG guidelines applied."
    )
    fhir_resource_type: str = Field(description="The FHIR Resource Type (e.g., 'Patient', 'Encounter')")
    fhir_json: Dict[str, Any] = Field(
        description="The final mapped FHIR resource in strict JSON format."
    )

class FHIRCriticOutput(BaseModel):
    """Critic 에이전트의 출력 스키마. 검증 결과와 피드백을 제공합니다."""
    is_valid: bool = Field(description="True if the fhir_json strictly follows FHIR R4 standards.")
    error_category: Optional[str] = Field(
        default=None, 
        description="Category of error: 'Missing Required Field', 'Type Mismatch', 'Terminology Error', etc."
    )
    feedback: str = Field(
        description="If is_valid is False, provide specific, actionable feedback for the Mapper. "
                    "If True, simply output 'PASS'."
    )

# ==========================================
# 2. 에이전트 클래스 정의
# ==========================================

class FHIRMultiAgentPipeline:
    def __init__(self, llm_model: ChatOllama, rag_retriever=None):
        self.llm = llm_model
        self.rag_retriever = rag_retriever # FHIR IG 및 표준 용어 검색용 RAG
        
        # Mapper Chain 세팅
        self.mapper_prompt = ChatPromptTemplate.from_template("""
        You are an expert HL7 FHIR Data Mapper.
        Map the following MIMIC-IV raw data to a FHIR R4 {target_resource} resource.
        
        [Input Data]
        Schema: {schema}
        Raw Data: {raw_data}
        
        [RAG Knowledge (FHIR Guidelines & Terminology)]
        {rag_context}
        
        [Critic Feedback from previous attempt]
        {previous_feedback}
        
        Follow the FHIRMapperOutput schema. MUST explain your thought process first.
        """)
        self.mapper_chain = self.mapper_prompt | self.llm.with_structured_output(FHIRMapperOutput)
        
        # Critic Chain 세팅
        self.critic_prompt = ChatPromptTemplate.from_template("""
        You are a strict HL7 FHIR Verifier.
        Review the generated FHIR JSON for the {target_resource} resource against FHIR R4 specifications.
        
        [RAG Knowledge (Validation Rules)]
        {rag_context}
        
        [Generated JSON]
        {fhir_json}
        
        Check for missing required fields (e.g., status, subject), incorrect data types, and invalid codes.
        """)
        self.critic_chain = self.critic_prompt | self.llm.with_structured_output(FHIRCriticOutput)

    def retrieve_knowledge(self, query: str) -> str:
        """RAG 시스템에서 지식을 검색하는 함수 (Mock)"""
        if not self.rag_retriever:
            return "No external RAG context provided. Rely on internal FHIR R4 knowledge."
        # 실제 구현 시: return self.rag_retriever.invoke(query)
        return "FHIR R4 Patient resource requires 'gender' (male|female|other|unknown) and 'birthDate' (YYYY-MM-DD)."

    def run_conversion_task(self, task: Dict[str, Any], max_retries: int = 3) -> Optional[Dict[str, Any]]:
        """단일 테이블/데이터에 대한 변환 및 자가 수정 루프 실행"""
        target_resource = task['target_resource']
        schema = task['schema']
        raw_data = task['data']
        
        # RAG를 통해 필요한 지식 미리 로드
        rag_context = self.retrieve_knowledge(f"FHIR R4 {target_resource} resource structure and required fields")
        
        previous_feedback = "None. This is the first attempt."
        is_success = False
        attempt = 0
        final_resource = None
        
        print(f"\n▶ Starting Conversion: {task['table_name']} -> {target_resource}")
        
        # Self-Correction Loop
        while not is_success and attempt < max_retries:
            attempt += 1
            print(f"  ↳ Attempt {attempt}/{max_retries}...")
            
            try:
                # 1. Mapper Agent 호출
                mapped_result: FHIRMapperOutput = self.mapper_chain.invoke({
                    "target_resource": target_resource,
                    "schema": json.dumps(schema),
                    "raw_data": json.dumps(raw_data),
                    "rag_context": rag_context,
                    "previous_feedback": previous_feedback
                })
                
                print(f"    [Mapper] CoT: {mapped_result.thought_process[:100]}...")
                
                verification: FHIRCriticOutput = self.critic_chain.invoke({
                    "target_resource": target_resource,
                    "rag_context": rag_context,
                    "fhir_json": json.dumps(mapped_result.fhir_json)
                })
                
                # 3. 결과 판단 및 피드백 루프
                if verification.is_valid:
                    print("    [Critic] ✓ PASS. Resource is valid.")
                    final_resource = mapped_result.fhir_json
                    is_success = True
                else:
                    print(f"    [Critic] ✗ FAIL ({verification.error_category}): {verification.feedback}")
                    # 실패 시 피드백을 업데이트하여 다음 루프에 전달
                    previous_feedback = verification.feedback
                    
            except Exception as e:
                print(f"    [System] Error during agent execution: {e}")
                previous_feedback = f"System Error occurred: {str(e)}. Please try mapping again carefully."
                
        if not is_success:
            print(f"⚠ Failed to map {target_resource} after {max_retries} attempts. Requires human review.")
            
        return final_resource


def orchestrate_patient_conversion(subject_id: int):
    """Manager 역할을 수행하는 메인 파이프라인 함수"""
    print(f"=== Orchestrating FHIR Conversion for Patient {subject_id} ===")
    
    # LLM 초기화 (기존 테스트 코드 참고)
    llm = ChatOllama(model='gpt-oss:20b', base_url="http://localhost:11434", temperature=0)
    pipeline = FHIRMultiAgentPipeline(llm_model=llm)
    
    # [가상 데이터] DB에서 조회해온 In-memory Payload
    tasks = [
        {
            "table_name": "mimiciv_hosp.patients",
            "target_resource": "Patient",
            "schema": {
                "subject_id": "int", "gender": "varchar", "anchor_age": "int"
            },
            "data": {
                "subject_id": subject_id, "gender": "F", "anchor_age": 52
            }
        }
        # 여기에 admissions -> Encounter, prescriptions -> MedicationRequest 등을 추가
    ]
    
    fhir_bundle = {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": []
    }
    
    # 작업을 하위 에이전트(Mapper->Critic 루프)에 위임 (Task Decomposition)
    for task in tasks:
        resource = pipeline.run_conversion_task(task, max_retries=3)
        if resource:
            fhir_bundle["entry"].append({
                "resource": resource,
                "request": {"method": "POST", "url": task['target_resource']}
            })
            
    print("\n=== Final FHIR Bundle Generated ===")
    print(json.dumps(fhir_bundle, indent=2))
    return fhir_bundle

# 실행
if __name__ == "__main__":
    orchestrate_patient_conversion(10000032)
    # pass