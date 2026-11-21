"""실험 설정 잠금과 산출물의 내용 기반 식별자. DB 접속 정보는 저장하지 않습니다."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]


def fingerprint(value: Any) -> str:
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256()
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    content = json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                     prefix=f".{destination.name}.", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def code_fingerprint() -> str:
    files = sorted((ROOT / "research").rglob("*.py"))
    files += [ROOT / "fhir_agent_pipeline" / name for name in (
        "fhir_schema.py", "fhir_models.py", "json_utils.py", "resource_registry.py", "validation.py", "ollama.py",
    )]
    return fingerprint({str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files})


def _integer(section: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key}는 {minimum}~{maximum} 범위의 정수여야 합니다.")
    return value


def validate_experiment_config(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        raise ValueError("실험 설정은 JSON 객체여야 합니다.")
    for key in ("model", "prompts", "rag", "correction", "generation", "validation"):
        if not isinstance(config.get(key), dict):
            raise ValueError(f"실험 설정에 {key} 객체가 필요합니다.")
    model = config["model"]
    if not isinstance(model.get("name"), str) or not model["name"].strip():
        raise ValueError("model.name이 필요합니다.")
    parsed = urlparse(str(model.get("base_url", "")))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("model.base_url에는 인증정보 없는 HTTP(S) 주소가 필요합니다.")
    temperature = model.get("temperature", 0)
    if isinstance(temperature, bool) or not isinstance(temperature, (float, int)) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        raise ValueError("model.temperature는 0~2의 유한한 숫자여야 합니다.")
    _integer(model, "seed", 42, 0, 2**31 - 1)
    _integer(model, "timeout_seconds", 300, 1, 86400)
    _integer(model, "context_size", 8192, 256, 2**24)
    _integer(model, "max_output_tokens", 4096, 1, 2**24)
    if "num_gpu" in model:
        _integer(model, "num_gpu", 0, 0, 2**31 - 1)
    thinking = model.get("think", False)
    if not isinstance(thinking, bool) and thinking not in ("low", "medium", "high"):
        raise ValueError("model.think는 boolean 또는 low/medium/high여야 합니다.")
    rag = config["rag"]
    if not isinstance(rag.get("enabled", True), bool):
        raise ValueError("rag.enabled는 boolean이어야 합니다.")
    for key in ("source_contract", "source_enabled"):
        if not isinstance(rag.get(key, False), bool):
            raise ValueError(f"rag.{key}는 boolean이어야 합니다.")
    if rag.get("source_enabled") and (not rag.get("enabled", True) or not rag.get("source_contract")):
        raise ValueError("원천 RAG에는 FHIR RAG와 명시적 원천 스키마 계약이 필요합니다.")
    if rag.get("source_contract") and config["generation"].get("sample_rows") != 0:
        raise ValueError("스키마 전용 초기 생성에는 generation.sample_rows=0이 필요합니다.")
    _integer(rag, "source_top_k", 6, 1, 64)
    if "fhir_constraints_path" in rag and (not isinstance(rag["fhir_constraints_path"], str)
                                          or not rag["fhir_constraints_path"].strip()):
        raise ValueError("rag.fhir_constraints_path는 로컬 R4 패키지 경로여야 합니다.")
    if rag.get("enabled", True) and not str(rag.get("embedding_model", "")).strip():
        raise ValueError("RAG에는 rag.embedding_model이 필요합니다.")
    _integer(rag, "top_k", 6, 1, 64)
    _integer(rag, "embedding_batch_size", 16, 1, 256)
    _integer(rag, "max_document_chars", 2000, 256, 16000)
    if "num_gpu" in rag:
        _integer(rag, "num_gpu", 0, 0, 2**31 - 1)
    _integer(config["generation"], "batch_size", 0, 1, 10000)
    _integer(config["generation"], "sample_rows", 3, 0, 1000)
    _integer(config["generation"], "max_attempts", 2, 1, 10)
    if not isinstance(config["generation"].get("managed_identity", False), bool):
        raise ValueError("generation.managed_identity는 boolean이어야 합니다.")
    _integer(config["correction"], "max_iterations", -1, 0, 20)
    for section in (config, config["correction"]):
        for key in ("gold_feedback", "use_gold_feedback", "allow_gold_feedback"):
            if section.get(key) not in (None, False):
                raise ValueError("독립 평가 정답을 Profile 생성·교정에 사용할 수 없습니다.")
    roles = config.get("preparation_roles")
    if roles is not None:
        allowed = {"mimic_iv": {"development", "validation"}, "synthea": {"calibration"}}
        if not isinstance(roles, dict) or set(roles) - set(allowed):
            raise ValueError("preparation_roles에는 mimic_iv/synthea만 지정할 수 있습니다.")
        for dataset, values in roles.items():
            if not isinstance(values, list) or not values or set(values) - allowed[dataset]:
                raise ValueError(f"{dataset}의 평가 환자는 Profile 준비에 사용할 수 없습니다.")
    validation = config["validation"]
    if not validation.get("schema_path") or not (ROOT / validation["schema_path"]).is_file():
        raise ValueError("validation.schema_path에 실제 FHIR Schema 파일이 필요합니다.")
    if not isinstance(validation.get("require_hl7", False), bool):
        raise ValueError("validation.require_hl7는 boolean이어야 합니다.")
    jar = validation.get("validator_jar")
    if jar and not (ROOT / jar).is_file():
        raise ValueError("validation.validator_jar 파일이 없습니다.")
    if validation.get("require_hl7", False) and not jar:
        raise ValueError("HL7 필수 실험에는 validation.validator_jar가 필요합니다.")
    _integer(validation, "timeout_seconds", 120, 1, 86400)
    analysis = config.get("analysis", {})
    if not isinstance(analysis, dict):
        raise ValueError("analysis는 객체여야 합니다.")
    _integer(analysis, "resamples", 2000, 100, 100000)
    _integer(analysis, "seed", 42, 0, 2**31 - 1)
    width = analysis.get("target_half_width")
    if width is not None and (isinstance(width, bool) or not isinstance(width, (int, float)) or not 0 < width < 1):
        raise ValueError("analysis.target_half_width는 0~1 사이의 숫자여야 합니다.")
    checks = config.get("source_checks", {})
    if not isinstance(checks, dict) or any(not isinstance(value, dict) for value in checks.values()):
        raise ValueError("source_checks는 dataset별 resourceType 객체여야 합니다.")
    if any(not isinstance(value, list) for dataset in checks.values() for value in dataset.values()):
        raise ValueError("각 source_checks 항목은 assertion 배열이어야 합니다.")
    if "logging" in config and (not isinstance(config["logging"], dict) or not isinstance(config["logging"].get("record_payloads", False), bool)):
        raise ValueError("logging.record_payloads는 boolean이어야 합니다.")


def load_experiment_config(path: str | Path) -> dict[str, Any]:
    config = read_json(path)
    validate_experiment_config(config)
    config["prompt_texts"] = {
        role: (ROOT / location).read_text(encoding="utf-8")
        for role, location in config["prompts"].items()
    }
    if set(config["prompt_texts"]) != {"mapping", "direct"}:
        raise ValueError("mapping/direct 프롬프트가 필요합니다.")
    return config


def exact_fhir_constraints(config: dict[str, Any]):
    path = config["rag"].get("fhir_constraints_path")
    if path:
        from .fhir_constraints import FhirConstraintLookup
        return FhirConstraintLookup(ROOT / path)
    return None


def freeze_policy(config: dict[str, Any], *, model_identity: dict[str, Any], rag_identity: dict[str, Any]) -> dict[str, Any]:
    """MIMIC 개발 종료 시 호출합니다. 이후 dataset별 Profile만 새로 생성합니다."""
    validate_experiment_config(config)
    if not model_identity.get("digest") or not model_identity.get("name"):
        raise ValueError("설정 잠금에는 실제 모델 digest가 필요합니다.")
    schema_path = ROOT / config["validation"]["schema_path"]
    payload = {
        "format_version": 1,
        "developed_on": "mimic_iv",
        "sql_planning": False,
        "external_validation": "synthetic_cross_dataset",
        "config": config,
        "model_identity": model_identity,
        "rag_identity": rag_identity,
        "code_sha256": code_fingerprint(),
        "fhir_schema_sha256": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
    }
    jar = config["validation"].get("validator_jar")
    payload["validator_jar_sha256"] = hashlib.sha256((ROOT / jar).read_bytes()).hexdigest() if jar else None
    if config["rag"].get("enabled", True):
        if rag_identity.get("kind") != "fhir_schema_only" or not re.fullmatch(r"[0-9a-f]{64}", str(rag_identity.get("index_sha256", ""))):
            raise ValueError("FHIR 전용 RAG index의 실제 identity가 필요합니다.")
        if rag_identity.get("schema_sha256") != payload["fhir_schema_sha256"]:
            raise ValueError("RAG index와 검증용 FHIR Schema가 다릅니다.")
        if not rag_identity.get("embedding_identity", {}).get("digest"):
            raise ValueError("RAG index의 임베딩 모델 digest가 필요합니다.")
    elif rag_identity != {"enabled": False}:
        raise ValueError("RAG 비활성 정책에는 index를 연결할 수 없습니다.")
    if config["rag"].get("source_enabled"):
        from .source_rag import source_recipe
        payload["source_rag_recipe"] = source_recipe(top_k=int(config["rag"].get("source_top_k", 6)))
    constraints = exact_fhir_constraints(config)
    if constraints is not None:
        payload["fhir_constraints_identity"] = constraints.identity
    return {**payload, "policy_sha256": fingerprint(payload)}


def verify_policy(policy: dict[str, Any]) -> dict[str, Any]:
    unsigned = {key: value for key, value in policy.items() if key != "policy_sha256"}
    if fingerprint(unsigned) != policy.get("policy_sha256"):
        raise ValueError("잠금 설정의 내용/hash가 일치하지 않습니다.")
    validate_experiment_config(policy["config"])
    if policy.get("sql_planning") is not False or policy.get("developed_on") != "mimic_iv":
        raise ValueError("SQL Planning 없는 MIMIC 개발 정책이 아닙니다.")
    if code_fingerprint() != policy["code_sha256"]:
        raise ValueError("코드가 잠금 시점 이후 변경되었습니다. 새 개발 실험으로 다시 잠그세요.")
    schema = ROOT / policy["config"]["validation"]["schema_path"]
    if hashlib.sha256(schema.read_bytes()).hexdigest() != policy["fhir_schema_sha256"]:
        raise ValueError("FHIR Schema가 잠금 시점 이후 변경되었습니다.")
    jar = policy["config"]["validation"].get("validator_jar")
    actual = hashlib.sha256((ROOT / jar).read_bytes()).hexdigest() if jar else None
    if actual != policy.get("validator_jar_sha256"):
        raise ValueError("Validator가 잠금 시점 이후 변경되었습니다.")
    constraints = exact_fhir_constraints(policy["config"])
    if (constraints.identity if constraints else None) != policy.get("fhir_constraints_identity"):
        raise ValueError("FHIR 정확 조회 패키지가 잠금 시점 이후 변경되었습니다.")
    return policy
