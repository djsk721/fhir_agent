from copy import deepcopy
import json

import pytest

from research.fhir_profile.artifacts import fingerprint, write_json
from research.fhir_profile.source_rag import SourceRetriever, build_source_index, source_metadata, source_recipe


class FakeEmbedder:
    embedding_model = "fake"

    def __init__(self):
        self.documents = []
        self.queries = []

    def embed_documents(self, texts):
        self.documents.extend(texts)
        return [self._vector(sentence) for sentence in texts]

    def identity(self, model):
        return {"name": model, "digest": "fake-digest"}

    def embed_query(self, text):
        self.queries.append(text)
        return [1.0, 0.0]

    def _vector(self, sentence):
        if '"description":"Reviewed measured reading"' in sentence:
            return [0.99, 0.02]
        if '.unrelated' in sentence:
            return [1.0, 0.0]
        if '.subjects' in sentence:
            return [1.0, 0.0]
        return [0.0, 1.0]


def _spec(dataset="alpha"):
    return {
        "dataset": dataset, "dataset_version": "v0", "schema_version": "v1",
        "resources": {
            "Patient": {"schema": "example", "source_resource": "subjects",
                        "columns": ["subject_key"], "primary_keys": ["subject_key"]},
            "Observation": {
                "schema": "example", "source_resource": "readings",
                "columns": ["key", "subject", "raw_code", "result"],
                "primary_keys": ["key"], "identity_fields": ["key"],
                "foreign_keys": [
                    {"columns": ["subject"], "referred_table": "subjects", "referred_columns": ["subject_key"]},
                    {"columns": ["raw_code"], "referred_table": "external_dictionary", "referred_columns": ["code"]},
                ],
                "column_types": {"result": "decimal"},
                "column_nullable": {"result": True},
                "column_descriptions": {"result": "Reviewed measured reading"},
                "sample_records": [{"result": "NEVER_INDEX_THIS_PATIENT_VALUE"}],
                "mapping_gold": "NEVER_INDEX_THIS_ANSWER",
            },
            "Condition": {"schema": "example", "source_resource": "unrelated", "columns": ["some_column"]},
        },
    }


def _cohort(spec):
    return {
        "dataset": spec["dataset"], "extract_spec_sha256": fingerprint(spec),
        "resources": {
            kind: {"source_resource": item["source_resource"],
                   "metadata": {"columns": [*item["columns"], "extra_csv_header"]},
                   "records": [{"result": "NEVER_INDEX_THIS_PATIENT_VALUE"}]}
            for kind, item in spec["resources"].items()
        },
    }


def test_source_index_projects_metadata_and_separates_datasets(tmp_path):
    spec = _spec()
    client = FakeEmbedder()
    config = {"rag": {"embedding_batch_size": 2}}
    artifact = build_source_index(spec, config, client)
    assert build_source_index(spec, config, FakeEmbedder()) == artifact
    assert artifact["kind"] == "source_schema_only"
    assert artifact["extract_spec_sha256"] == fingerprint(spec)
    assert artifact["metadata"]["Observation"] == source_metadata(spec, "Observation")
    assert artifact["metadata"]["Observation"]["column_nullable"] == {"result": True}
    assert artifact["metadata"]["Observation"]["column_types"] == {"result": "decimal"}
    assert "NEVER_INDEX_THIS" not in json.dumps(artifact)
    assert "NEVER_INDEX_THIS" not in " ".join(client.documents)
    with pytest.raises(ValueError, match="prepared cohort"):
        build_source_index({**_cohort(spec), "patient_keys": ["example"]}, config, client)
    first = tmp_path / "first.json"
    other = tmp_path / "other.json"
    write_json(first, artifact)
    write_json(other, build_source_index(_spec("beta"), config, FakeEmbedder()))
    a = SourceRetriever(first, client, top_k=1)
    b = SourceRetriever(other, FakeEmbedder(), top_k=1)
    assert a.identity["index_sha256"] != b.identity["index_sha256"]
    assert a.identity["recipe"] == b.identity["recipe"] == source_recipe(top_k=1)
    a.verify_cohort(_cohort(spec))
    with pytest.raises(ValueError, match="dataset and extraction spec"):
        b.verify_cohort(_cohort(spec))
    missing = _cohort(spec)
    missing["resources"]["Observation"]["metadata"]["columns"].remove("result")
    with pytest.raises(ValueError, match="columns"):
        a.verify_cohort(missing)


def test_retrieval_has_exact_fks_and_cannot_search_an_unrelated_table(tmp_path):
    spec = _spec()
    client = FakeEmbedder()
    path = tmp_path / "schema.json"
    write_json(path, build_source_index(spec, {"rag": {}}, client))
    retriever = SourceRetriever(path, client, top_k=1)
    evidence = retriever.context("Observation")
    assert evidence["exact_schema"]["primary_keys"] == ["key"]
    assert evidence["exact_schema"]["columns"] == ["key", "subject", "raw_code", "result"]
    identity_link, external_link = evidence["foreign_key_neighbors"]
    assert identity_link["target_in_index"]
    assert identity_link["target_relation"] == "example.subjects"
    assert identity_link["target_primary_keys"] == ["subject_key"]
    assert not external_link["target_in_index"]
    assert evidence["retrieved_documents"][0]["path"] == "example.readings.result"
    evidence["exact_schema"]["columns"].clear()
    assert retriever.context("Observation")["exact_schema"]["columns"]
    assert client.queries == [
        "Source schema example.readings for Observation: source field meanings, column types, primary keys, and foreign key references."
    ]
    two = SourceRetriever(path, client, top_k=4).context("Observation")
    assert {doc["resource_type"] for doc in two["retrieved_documents"]} == {"Patient", "Observation"}


def test_index_rejects_tampering_and_row_payloads_even_after_rehash(tmp_path):
    artifact = build_source_index(_spec(), {"rag": {}}, FakeEmbedder())
    path = tmp_path / "schema.json"
    corrupted = deepcopy(artifact)
    corrupted["documents"][0]["content"] = "hallucinated"
    write_json(path, corrupted)
    with pytest.raises(ValueError, match="index hash"):
        SourceRetriever(path, FakeEmbedder())
    corrupted["index_sha256"] = fingerprint({k: v for k, v in corrupted.items() if k != "index_sha256"})
    write_json(path, corrupted)
    with pytest.raises(ValueError, match="document and vector"):
        SourceRetriever(path, FakeEmbedder())
    corrupted = deepcopy(artifact)
    corrupted["metadata"]["Observation"]["rows"] = ["not schema"]
    corrupted["index_sha256"] = fingerprint({k: v for k, v in corrupted.items() if k != "index_sha256"})
    write_json(path, corrupted)
    with pytest.raises(ValueError, match="outside the reviewed schema"):
        SourceRetriever(path, FakeEmbedder())


def test_metadata_rejects_invented_foreign_key_and_invalid_target_scope():
    spec = _spec()
    spec["resources"]["Observation"]["foreign_keys"][0]["columns"] = ["undeclared"]
    with pytest.raises(ValueError, match="declared source column"):
        source_metadata(spec, "Observation")
    spec = _spec()
    spec["resources"]["Observation"]["foreign_keys"][0]["referred_columns"] = ["undeclared"]
    with pytest.raises(ValueError, match="referenced columns"):
        build_source_index(spec, {"rag": {}}, FakeEmbedder())
