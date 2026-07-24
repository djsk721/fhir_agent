from dataclasses import replace
from pathlib import Path

from fhir_agent_pipeline.config import PROJECT_ROOT, load_settings
from fhir_agent_pipeline.fhir_models import build_bundle
from fhir_agent_pipeline.llm_query_service import LlmClinicalQueryService
from fhir_agent_pipeline.mapping_config import SourceMappingConfig
from fhir_agent_pipeline.source_profile import SourceProfile


def test_hospital_profile_changes_physical_sql_without_pipeline_changes() -> None:
    profile = SourceProfile.load(
        PROJECT_ROOT / "profiles" / "hospital_sample" / "profile.json"
    )
    mapping = SourceMappingConfig.load(profile.mapping_path)
    settings = replace(
        load_settings(require_database=False),
        source_profile_path=profile.source_path,
        mimic_mapping_config_path=profile.mapping_path,
        mimic_schema=profile.database_schema,
        mimic_patients_table=profile.table("patient"),
        mimic_admissions_table=profile.table("encounter"),
        mimic_labevents_table=profile.table("observation"),
        mimic_diagnoses_table=profile.table("condition"),
        mimic_diagnosis_dictionary_table=profile.table(
            "condition_dictionary"
        ),
        mimic_procedures_table=profile.table("procedure"),
        mimic_procedure_dictionary_table=profile.table(
            "procedure_dictionary"
        ),
    )
    service = LlmClinicalQueryService(
        settings, mapping, sql_agent=object(), profile=profile  # type: ignore[arg-type]
    )

    question = service._patient_question("P-001", [], None)

    assert '"clinical_cdw"."patient_master"' in question
    assert "patient_no = 'P-001'" in question
    assert "subject_id" not in question
    assert "mimiciv_hosp" not in question


def test_bundle_namespace_is_source_profile_policy() -> None:
    resource = {"resourceType": "Patient", "id": "P-001"}

    first = build_bundle([resource], namespace="hospital-a")
    second = build_bundle([resource], namespace="hospital-b")

    assert first["entry"][0]["fullUrl"] != second["entry"][0]["fullUrl"]


def test_profile_paths_resolve_relative_to_profile_file() -> None:
    profile_path = (
        PROJECT_ROOT / "profiles" / "mimic_iv" / "profile.json"
    )
    profile = SourceProfile.load(Path(profile_path))

    assert profile.mapping_path == (
        PROJECT_ROOT / "db" / "mimic_mapping_config.json"
    )
    assert profile.query_for("Observation").source_table_role == "observation"
