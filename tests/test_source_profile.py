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


def test_settings_have_no_mapping_path() -> None:
    settings = load_settings(require_database=False)
    assert not hasattr(settings, "source_mapping_config_path")
