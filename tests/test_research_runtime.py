from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from research.fhir_profile.artifacts import ROOT, fingerprint, freeze_policy, load_experiment_config, verify_policy, write_json
from research.fhir_profile.runtime import FhirRetriever, ResearchOllamaClient, schema_documents


def test_explicit_cpu_options_apply_to_chat_and_embeddings(monkeypatch):
    settings = config()
    settings["model"]["num_gpu"] = 0
    settings["rag"]["num_gpu"] = 0
    client = ResearchOllamaClient(settings)
    requests = {}

    def post(path, payload):
        requests[path] = payload
        if path == "/api/embed":
            return {"embeddings": [[1.0, 2.0]]}
        return {"message": {"content": "{}"}}

    monkeypatch.setattr(client, "_post", post)
    client.chat("system", "test")
    client.embed_documents(["schema"])
    assert requests["/api/chat"]["options"]["num_gpu"] == 0
    assert requests["/api/embed"]["options"] == {"num_gpu": 0}
    assert client.calls[-1]["options"] == {"num_gpu": 0}


def config():
    value = load_experiment_config(ROOT / "experiments/configs/pilot.json")
    value["validation"].update({"require_hl7": False, "validator_jar": None})
    return value


def test_chat_metrics_use_actual_api_statistics_and_frozen_options():
    client = ResearchOllamaClient(config())
    requests = []
    def post(endpoint, payload):
        requests.append((endpoint, payload))
        return {"message": {"content": '{"resources":[]}'}, "prompt_eval_count": 37, "eval_count": 9}
    client._post = post
    assert client.chat("system", "input") == '{"resources":[]}'
    assert requests[0][1]["options"]["seed"] == 42
    assert client.calls[0]["prompt_tokens"] == 37
    assert client.calls[0]["output_tokens"] == 9
    assert client.calls[0]["elapsed_seconds"] >= 0


def test_failed_chat_is_recorded_without_prompt_values():
    client = ResearchOllamaClient(config())
    def fail(*args, **kwargs):
        raise RuntimeError("offline")
    client._post = fail
    with pytest.raises(RuntimeError):
        client.chat("system", "private record")
    assert client.calls[0]["success"] is False
    assert "private record" not in str(client.calls)


def test_fhir_documents_are_bounded_and_contain_no_source_mapping():
    documents = schema_documents(ROOT / "db/fhir.schema.json", max_document_chars=512)
    assert documents
    assert max(len(item["content"]) for item in documents) <= 512
    assert any(item["definition"] == "Observation" and item["kind"] == "overview" for item in documents)


def test_vector_retrieval_checks_hash_and_dimensions(tmp_path):
    client = ResearchOllamaClient(config())
    client.embed_query = lambda _: [1.0, 0.0]
    payload = {"format_version": 1, "kind": "fhir_schema_only", "dimension": 2,
               "schema_sha256": hashlib.sha256((ROOT / "db/fhir.schema.json").read_bytes()).hexdigest(),
               "embedding_identity": {"name": "nomic-embed-text", "digest": "fixture"},
               "documents": [{"definition": "Patient", "kind": "overview", "content": "FHIR Patient definition"},
                             {"definition": "Quantity", "kind": "overview", "content": "FHIR Quantity definition"}],
               "vectors": [[1.0, 0.0], [0.0, 1.0]]}
    index = {**payload, "index_sha256": fingerprint(payload)}
    path = tmp_path / "index.json"
    write_json(path, index)
    retriever = FhirRetriever(path, client, top_k=1)
    assert "FHIR Patient" in retriever.context("Patient")
    client.embed_query = lambda _: [1.0]
    with pytest.raises(ValueError, match="차원"):
        retriever.context("Patient")
    index["vectors"][0] = [0.0, 0.0]
    write_json(path, index)
    with pytest.raises(ValueError, match="hash"):
        FhirRetriever(path, client)


def test_policy_rejects_gold_feedback_and_mismatched_schema_index():
    value = config()
    value["correction"]["gold_feedback"] = True
    with pytest.raises(ValueError, match="정답"):
        freeze_policy(value, model_identity={"name": "gemma4:e4b", "digest": "fixture"}, rag_identity={})
    value["correction"]["gold_feedback"] = False
    with pytest.raises(ValueError, match="Schema"):
        freeze_policy(value, model_identity={"name": "gemma4:e4b", "digest": "fixture"}, rag_identity={
            "kind": "fhir_schema_only", "index_sha256": "0" * 64, "schema_sha256": "wrong",
            "embedding_identity": {"digest": "fixture"}})
