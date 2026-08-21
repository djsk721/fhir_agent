from fhir_agent_pipeline.config import PROJECT_ROOT, load_settings
from fhir_agent_pipeline.fhir_models import build_bundle
from fhir_agent_pipeline.source_profile import SourceProfile


def test_profiles_share_source_neutral_query_contract() -> None:
    mimic = SourceProfile.load(
        PROJECT_ROOT / "profiles" / "mimic_iv" / "profile.json"
    )
    hospital = SourceProfile.load(
        PROJECT_ROOT / "profiles" / "hospital_sample" / "profile.json"
    )

    assert mimic.query_path == hospital.query_path
    assert mimic.query_path == (
        PROJECT_ROOT / "profiles" / "common" / "clinical_queries.json"
    )
    question = hospital.query_for("Patient").render(
        {
            "source_schemas": "clinical",
            "patient_key": "'P-001'",
        }
    )
    assert "_patient_key" in question
    assert "Do not assume any table or column name" in question
    assert "mimic" not in question.lower()


def test_bundle_namespace_is_source_profile_policy() -> None:
    resource = {"resourceType": "Patient", "id": "P-001"}
    assert (
        build_bundle([resource], namespace="hospital-a")["entry"][0]["fullUrl"]
        != build_bundle([resource], namespace="hospital-b")["entry"][0]["fullUrl"]
    )


def test_observation_query_is_patient_wide() -> None:
    profile = SourceProfile.load(
        PROJECT_ROOT / "profiles" / "mimic_iv" / "profile.json"
    )
    question = profile.query_for("Observation").render({
        "source_schemas": "clinical",
        "patient_key": "7",
        "encounter_keys": "11, 12",
        "limit": 100,
    })

    assert "patient-wide SELECT" in question
    assert "broadest hospital-wide laboratory" in question
    assert "over ICU-only chart" in question
    assert "documented unique event-row identifier" in question
    assert "MUST NOT concatenate other columns" in question
    assert "Do not use a comments" in question
    assert "do not restrict observations to encounter" in question
    assert "including rows whose encounter key is NULL" in question
    assert "returning a literal NULL for _encounter_key" in question
    assert "an INNER JOIN is forbidden" in question
    assert "Never include an optional encounter" in question
    assert "encounter keys in (11, 12)" not in question


def test_encounter_and_clinical_queries_require_full_encounter_scope() -> None:
    profile = SourceProfile.load(
        PROJECT_ROOT / "profiles" / "mimic_iv" / "profile.json"
    )
    values = {
        "source_schemas": "clinical",
        "patient_key": "7",
        "encounter_keys": "11, 12",
        "encounter_override_clause": "",
        "limit": 100,
    }

    encounter = profile.query_for("Encounter").render(values)
    assert "broadest single patient-level relation" in encounter
    assert "Return every matching encounter" in encounter
    assert "ICU-stay-only" in encounter
    assert "never concatenate it with a stay" in encounter
    assert "CASE expression, COALESCE fallback" in encounter

    for resource_type in ("Condition", "Procedure"):
        question = profile.query_for(resource_type).render(values)
        assert "all encounter keys in (11, 12)" in question
        assert f"do not select only one encounter" in question
        assert f"Preserve every matching" in question
        assert "an INNER JOIN or a partial-key join is forbidden" in question
        assert "partial-key join is forbidden" in question
        assert "return NULL AS _display" in question

    procedure = profile.query_for("Procedure").render(values)
    assert "hospital encounter-level relation" in procedure
    assert "procedure code and its code version" in procedure
    assert "over ICU-only procedure events" in procedure
    assert "diagnoses or conditions dictionary" in procedure
    assert "do not hash it" in procedure
    assert "return NULL AS _code_system" in procedure


def test_settings_have_no_mapping_path() -> None:
    settings = load_settings(require_database=False)
    assert not hasattr(settings, "source_mapping_config_path")
