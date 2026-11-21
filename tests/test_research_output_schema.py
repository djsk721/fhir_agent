import json
from copy import deepcopy

from jsonschema import Draft202012Validator
import pytest

from research.fhir_profile.profiles.output_schema import profile_generation_schema, profile_output_schema
from research.fhir_profile.profiles.schema import validate_profile
from research.fhir_profile.runtime import ResearchOllamaClient
from tests.test_research_runtime import config


def _profile(resource_type, mapping):
    return {
        "profile_id": "fixture", "profile_version": "1", "fhir_version": "4.0.1",
        "target_resource": resource_type, "source": {"dataset": "fixture", "table": "events"},
        "mappings": [mapping],
    }


@pytest.mark.parametrize(
    ("resource_type", "valid_rules", "invalid_rules"),
    [
        ("Encounter", ("coding:class", "string:class.code"),
         ("coding:class.coding.0", "coding:class.code", "codeable_concept:class", "integer:status")),
        ("Observation", ("decimal:valueQuantity.value", "string:valueString", "datetime:effectiveDateTime",
                         "date:effectiveDateTime", "coding:component.2.code.coding.1", "reference:encounter"),
         ("decimal:valueString", "boolean:valueQuantity.value", "coding:component.code.coding.0",
          "coding:component.999.code.coding.0", "string:valueMadeUp", "datetime:valueString")),
    ],
)
def test_wire_target_rules_couple_fhir_path_and_datatype(resource_type, valid_rules, invalid_rules):
    schema = profile_generation_schema(resource_type, wire=True)
    Draft202012Validator.check_schema(schema)
    rule_schema = schema["properties"]["mappings"]["items"]["properties"]["target_rule"]
    validator = Draft202012Validator(rule_schema)
    for encoded in valid_rules:
        assert validator.is_valid(encoded), encoded
    for encoded in invalid_rules:
        assert not validator.is_valid(encoded), encoded
    assert len(rule_schema["pattern"]) < 25_000


def test_runtime_canonicalizes_wire_and_applies_managed_identity(monkeypatch):
    metadata = {"columns": ["label"], "identity_contract": True, "record_identity_fields": ["_fixture_locator"]}
    wire = _profile("Observation", {"id": "value", "target_rule": "string:valueString", "source_field": "label"})
    with pytest.raises(ValueError, match="타입"):
        validate_profile(
            {**wire, "identity": {"source_fields": ["_fixture_locator"]},
             "mappings": [{"id": "value", "target_path": "valueString", "operation": "decimal", "source_field": "label"}]},
            metadata,
        )

    client = ResearchOllamaClient(config())
    sent = []
    def post(_, request):
        sent.append(request)
        return {"message": {"content": json.dumps(wire)}, "done_reason": "stop"}

    monkeypatch.setattr(client, "_post", post)
    prompt = json.dumps({"target_resource": "Observation", "source_metadata": metadata})
    answer = json.loads(client.chat_profile("system", prompt))
    assert "identity" not in sent[0]["format"]["required"]
    assert "identity" not in sent[0]["format"]["properties"]
    assert "target_rule" in json.loads(sent[0]["messages"][1]["content"])["decoder_output_contract"]["mapping_shape"]
    assert answer["identity"] == {"source_fields": ["_fixture_locator"]}
    assert answer["mappings"] == [
        {"id": "value", "target_path": "valueString", "operation": "string", "source_field": "label"}
    ]
    Draft202012Validator(profile_output_schema("Observation", metadata)).validate(answer)

    wire["mappings"][0]["target_rule"] = "coding:valueString"
    with pytest.raises(ValueError, match="output contract"):
        client.chat_profile("system", prompt)

    canonical = deepcopy(answer)
    canonical["identity"] = {"source_fields": ["label"]}
    wire.clear()
    wire.update(canonical)
    canonical_answer = json.loads(client.chat_profile("system", prompt))
    assert canonical_answer["identity"] == {"source_fields": ["_fixture_locator"]}
