"""A separate, schema-only retrieval index for the reviewed source scope."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re
from typing import Any

from .artifacts import fingerprint, read_json
from .runtime import _validate_vectors


INDEX_KIND = "source_schema_only"
_INDEX_RECIPE = "source-table-and-column-v1"
_LEXICAL_WEIGHT = 0.15


def source_recipe(*, top_k: int = 6) -> dict[str, Any]:
    if type(top_k) is not int or not 1 <= top_k <= 64:
        raise ValueError("source_top_k must be an integer between 1 and 64")
    return {
        "index": _INDEX_RECIPE, "ranker": "cosine-and-schema-token-v1", "top_k": top_k,
        "lexical_weight": _LEXICAL_WEIGHT, "scope": "target-and-outgoing-fk-neighbors",
        "minimum_target_documents": 1,
    }


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _names(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list of column names")
    names = [_string(item, label) for item in value]
    if len(set(names)) != len(names):
        raise ValueError(f"{label} contains duplicate column names")
    return names


def _shape_metadata(entry: dict[str, Any]) -> dict[str, Any]:
    """Copy only reviewed schema fields; do not infer anything from row values."""
    source = _string(entry.get("source_resource"), "source_resource")
    column_names = _names(entry.get("columns"), "columns")
    if not column_names:
        raise ValueError("source schema columns cannot be empty")
    allowed = set(column_names)
    result: dict[str, Any] = {
        "source_resource": source,
        "table": _string(entry.get("table", source), "table"),
        "columns": column_names,
        "primary_keys": [],
        "foreign_keys": [],
    }
    if entry.get("schema") is not None:
        result["schema"] = _string(entry["schema"], "schema")
    if entry.get("table_description") is not None:
        result["table_description"] = _string(entry["table_description"], "table_description")
    for label in ("primary_keys", "identity_fields", "sample_group_fields"):
        if label in entry:
            result[label] = _names(entry[label], label)
            if set(result[label]) - allowed:
                raise ValueError(f"{label} contains columns outside the reviewed source scope")
    for label in ("patient_column", "encounter_column"):
        if entry.get(label) is not None:
            result[label] = _string(entry[label], label)
            if result[label] not in allowed:
                raise ValueError(f"{label} is outside the reviewed source scope")
    for label in ("column_descriptions", "column_types", "column_nullable"):
        if label not in entry:
            continue
        field_map = entry[label]
        if not isinstance(field_map, dict) or set(field_map) - allowed:
            raise ValueError(f"{label} must describe reviewed source columns only")
        for value in field_map.values():
            if label == "column_nullable":
                if type(value) is not bool:
                    raise ValueError("column_nullable accepts explicit booleans only")
            else:
                _string(value, label)
        result[label] = dict(field_map)
    foreign_keys = entry.get("foreign_keys", [])
    if not isinstance(foreign_keys, list):
        raise ValueError("foreign_keys must be a list")
    for fk in foreign_keys:
        if not isinstance(fk, dict):
            raise ValueError("foreign key must be an object")
        left = _names(fk.get("columns"), "foreign_key.columns")
        right = _names(fk.get("referred_columns"), "foreign_key.referred_columns")
        if not left or len(left) != len(right) or set(left) - allowed:
            raise ValueError("foreign key must specify one declared source column per referenced column")
        link = {"columns": left, "referred_columns": right,
                "referred_table": _string(fk.get("referred_table"), "foreign_key.referred_table")}
        if fk.get("referred_schema") is not None:
            link["referred_schema"] = _string(fk["referred_schema"], "foreign_key.referred_schema")
        if "nullable" in fk:
            if not isinstance(fk["nullable"], bool):
                raise ValueError("foreign_key.nullable accepts explicit booleans only")
            link["nullable"] = fk["nullable"]
        result["foreign_keys"].append(link)
    return result


def source_metadata(spec: dict[str, Any], resource_type: str) -> dict[str, Any]:
    """Reviewed source metadata shared by inline and retriever-based experiments."""
    resources = spec.get("resources") if isinstance(spec, dict) else None
    if not isinstance(resources, dict) or not isinstance(resources.get(resource_type), dict):
        raise ValueError(f"no reviewed source metadata for {resource_type}")
    return _shape_metadata(resources[resource_type])


def _qualify(meta: dict[str, Any]) -> str:
    return ".".join(value for value in (meta.get("schema"), meta["table"]) if value is not None)


def _neighbor(metadata: dict[str, Any], origin: dict[str, Any], link: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    matches = [(target, meta) for target, meta in metadata.items()
               if link["referred_table"] in {meta["table"], meta["source_resource"]}
               and (not link.get("referred_schema") or link["referred_schema"] == meta.get("schema"))]
    if len(matches) > 1 and not link.get("referred_schema"):
        preferred = [(target, meta) for target, meta in matches if meta.get("schema") == origin.get("schema")]
        if preferred:
            matches = preferred
    if not matches:
        return None
    if len(matches) > 1:
        raise ValueError(f"ambiguous source foreign-key target {link['referred_table']}; specify referred_schema")
    target, meta = matches[0]
    if set(link["referred_columns"]) - set(meta["columns"]):
        raise ValueError(f"source foreign-key target {_qualify(meta)} does not declare its referenced columns")
    return target, meta


def _exact_context(metadata: dict[str, Any], resource_type: str) -> tuple[dict[str, Any], set[str]]:
    target = metadata[resource_type]
    allowed = {resource_type}
    branches = []
    for link in target["foreign_keys"]:
        neighbor = _neighbor(metadata, target, link)
        branches.append({
            **link,
            "target_in_index": neighbor is not None,
            **({"target_relation": _qualify(neighbor[1]),
                "target_primary_keys": neighbor[1]["primary_keys"],
                "target_identity_fields": neighbor[1].get("identity_fields", [])} if neighbor else {}),
        })
        if neighbor:
            allowed.add(neighbor[0])
    exact_fields = ("source_resource", "table", "schema", "columns", "primary_keys", "identity_fields")
    return {"exact_schema": {key: target[key] for key in exact_fields if key in target},
            "foreign_key_neighbors": branches}, allowed


def exact_source_context(spec: dict[str, Any], resource_type: str) -> dict[str, Any]:
    metadata = {kind: source_metadata(spec, kind) for kind in spec["resources"]}
    return _exact_context(metadata, resource_type)[0]


def _index_documents(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    docs = []
    for resource_type, shape in sorted(metadata.items()):
        qualified = _qualify(shape)
        for fk in shape["foreign_keys"]:
            _neighbor(metadata, shape, fk)
        for column in [None, *shape["columns"]]:
            if column is None:
                details = {key: shape[key] for key in ("columns", "primary_keys", "foreign_keys", "table_description")
                           if key in shape}
            else:
                details = {"column": column, "primary_key": column in shape["primary_keys"]}
                for field, alias in (("column_types", "type"), ("column_nullable", "nullable"),
                                     ("column_descriptions", "description")):
                    if column in shape.get(field, {}):
                        details[alias] = shape[field][column]
                links = [link for link in shape["foreign_keys"] if column in link["columns"]]
                if links:
                    details["foreign_keys"] = links
            path = qualified if column is None else f"{qualified}.{column}"
            content = (f"Source schema {resource_type} {path}\n"
                       + json.dumps(details, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            docs.append({"document_id": f"{resource_type}:{path}",
                         "resource_type": resource_type, "path": path,
                         "kind": "table" if column is None else "column", "content": content})
    return docs


def build_source_index(spec: dict[str, Any], config: dict[str, Any], client: Any) -> dict[str, Any]:
    """Index a reviewed extraction spec directly, not a prepared cohort or FHIR outputs."""
    resources = spec.get("resources") if isinstance(spec, dict) else None
    if not isinstance(resources, dict) or not resources or not all(isinstance(k, str) and k for k in resources):
        raise ValueError("source schema index requires named resources in an extraction spec")
    if any(field in spec for field in ("patient_keys", "cohort_sha256", "records")):
        raise ValueError("a prepared cohort is not a source schema index input")
    dataset = _string(spec.get("dataset"), "dataset")
    metadata = {kind: source_metadata(spec, kind) for kind in sorted(resources)}
    documents = _index_documents(metadata)
    size = config["rag"].get("embedding_batch_size", 16)
    if type(size) is not int or size < 1:
        raise ValueError("embedding batch size must be a positive integer")
    vectors = []
    for position in range(0, len(documents), size):
        texts = [doc["content"] for doc in documents[position:position + size]]
        batch = client.embed_documents(texts)
        if not isinstance(batch, list) or len(batch) != len(texts):
            raise ValueError("expected one embedding per source document")
        vectors.extend(batch)
    identity = client.identity(client.embedding_model)
    _check_identity(identity)
    payload = {
        "format_version": 1, "kind": INDEX_KIND, "document_recipe": _INDEX_RECIPE,
        "dataset": dataset, "extract_spec_sha256": fingerprint(spec),
        "embedding_identity": identity, "metadata": metadata,
        "documents": documents, "vectors": vectors, "dimension": _validate_vectors(vectors),
    }
    return {**payload, "index_sha256": fingerprint(payload)}


def _check_identity(identity: Any) -> None:
    if not isinstance(identity, dict) or set(identity) != {"name", "digest"}:
        raise ValueError("source embedding model identity must specify name and digest")
    _string(identity["name"], "embedding identity name")
    _string(identity["digest"], "embedding identity digest")


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", text.casefold()))


class SourceRetriever:
    def __init__(self, index_path: str | Path, client: Any, *, top_k: int = 6) -> None:
        index = read_json(index_path)
        if not isinstance(index, dict):
            raise ValueError("invalid source schema vector index")
        unsigned = {key: value for key, value in index.items() if key != "index_sha256"}
        if (index.get("kind") != INDEX_KIND or index.get("format_version") != 1
                or index.get("document_recipe") != _INDEX_RECIPE
                or fingerprint(unsigned) != index.get("index_sha256")):
            raise ValueError("source schema vector index hash, kind or recipe is invalid")
        if set(unsigned) != {"format_version", "kind", "document_recipe", "dataset", "extract_spec_sha256",
                             "embedding_identity", "metadata", "documents", "vectors", "dimension"}:
            raise ValueError("unexpected source schema vector index fields")
        _string(index["dataset"], "dataset")
        if not isinstance(index["extract_spec_sha256"], str) or not re.fullmatch("[a-f0-9]{64}", index["extract_spec_sha256"]):
            raise ValueError("invalid source extraction spec SHA-256")
        _check_identity(index["embedding_identity"])
        metadata = index["metadata"]
        if (not isinstance(metadata, dict) or not metadata
                or any(not kind or not isinstance(meta, dict) or _shape_metadata(meta) != meta
                       for kind, meta in metadata.items())):
            raise ValueError("source index contains metadata outside the reviewed schema fields")
        dimension = _validate_vectors(index["vectors"])
        if (index["documents"] != _index_documents(metadata) or len(index["vectors"]) != len(index["documents"])
                or type(index["dimension"]) is not int or dimension != index["dimension"]):
            raise ValueError("source index document and vector metadata disagree")
        self._index = index
        self.client = client
        self._recipe = source_recipe(top_k=top_k)
        self._normalized_vectors = [self._unit(vector) for vector in index["vectors"]]
        self._cache: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _unit(vector: list[float]) -> tuple[float, ...]:
        norm = math.hypot(*vector)
        return tuple(value / norm for value in vector)

    @property
    def recipe(self) -> dict[str, Any]:
        return dict(self._recipe)

    @property
    def identity(self) -> dict[str, Any]:
        fields = ("kind", "index_sha256", "dataset", "extract_spec_sha256", "embedding_identity")
        return {**{field: deepcopy(self._index[field]) for field in fields}, "recipe": self.recipe}

    def metadata(self, resource_type: str) -> dict[str, Any]:
        if resource_type not in self._index["metadata"]:
            raise ValueError(f"source schema index has no target {resource_type}")
        return deepcopy(self._index["metadata"][resource_type])

    def verify_cohort(self, cohort: dict[str, Any]) -> None:
        """Confirm index identity and scope before any retrieval. No patient rows are read."""
        if (cohort.get("dataset") != self._index["dataset"]
                or cohort.get("extract_spec_sha256") != self._index["extract_spec_sha256"]):
            raise ValueError("source schema index is not from this cohort dataset and extraction spec")
        resources = cohort.get("resources")
        if not isinstance(resources, dict) or set(resources) != set(self._index["metadata"]):
            raise ValueError("source schema index and cohort resource scopes differ")
        for resource_type, batch in resources.items():
            meta = self._index["metadata"][resource_type]
            batch_metadata = batch.get("metadata") if isinstance(batch, dict) else None
            batch_columns = batch_metadata.get("columns") if isinstance(batch_metadata, dict) else None
            if (not isinstance(batch_columns, list) or not all(isinstance(column, str) for column in batch_columns)
                    or batch.get("source_resource") != meta["source_resource"]
                    or set(meta["columns"]) - set(batch_columns)):
                raise ValueError(f"source schema index relation or columns do not match cohort {resource_type}")

    def context(self, resource_type: str) -> dict[str, Any]:
        if resource_type in self._cache:
            return deepcopy(self._cache[resource_type])
        target = self.metadata(resource_type)
        exact, allowed = _exact_context(self._index["metadata"], resource_type)
        query = (f"Source schema {_qualify(target)} for {resource_type}: source field meanings, "
                 "column types, primary keys, and foreign key references.")
        query_vector = self.client.embed_query(query)
        if _validate_vectors([query_vector]) != self._index["dimension"]:
            raise ValueError("source schema query and index vector dimensions differ")
        unit = self._unit(query_vector)
        words = _tokens(f"{resource_type} {_qualify(target)}")

        def rank(candidate: tuple[int, dict[str, Any]]) -> tuple[float, bool, str]:
            position, doc = candidate
            cosine = math.fsum(q * v for q, v in zip(unit, self._normalized_vectors[position]))
            lexical = len(words & _tokens(doc["path"])) / max(1, len(words))
            return (-(cosine + _LEXICAL_WEIGHT * lexical), doc["resource_type"] != resource_type, doc["document_id"])

        candidates = ((i, document) for i, document in enumerate(self._index["documents"])
                      if document["resource_type"] in allowed)
        ranked = sorted(candidates, key=rank)
        selected = ranked[:self._recipe["top_k"]]
        if not any(doc["resource_type"] == resource_type for _, doc in selected):
            best_target = next((candidate for candidate in ranked if candidate[1]["resource_type"] == resource_type))
            selected[-1] = best_target
        result = {
            **exact,
            "retrieved_documents": [doc for _, doc in selected],
        }
        self._cache[resource_type] = result
        return deepcopy(result)
