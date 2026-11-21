from __future__ import annotations

import copy
import csv
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from research.fhir_profile.data.extractors import CSVRecordExtractor, PredefinedQueryExtractor, load_extract_spec
from research.fhir_profile.splits import create_patient_split, patient_keys_for, validate_patient_split


ROOT = Path(__file__).resolve().parents[1]


def test_split_is_reproducible_independent_of_order_and_preserves_types() -> None:
    keys = [1, 2, 3, "uuid-a", "uuid-b", "uuid-c", 7, 8, 9, 10]
    split = create_patient_split(keys, "mimic_iv")
    assert split == create_patient_split(reversed(keys), "mimic_iv")
    assert validate_patient_split(split) == split
    assert sorted(map(len, split["roles"].values())) == [2, 2, 6]
    assert {type(key) for role in split["roles"].values() for key in role} == {str, int}
    assert len(patient_keys_for(split, "test")) == 2
    assert set(create_patient_split(keys, "synthea")["roles"]) == {"calibration", "test"}


def test_split_rejects_patient_leakage_and_modified_manifest() -> None:
    with pytest.raises(ValueError, match="중복"):
        create_patient_split([1, "1"], "mimic_iv")
    split = create_patient_split(list(range(10)), "mimic_iv")
    corrupted = copy.deepcopy(split)
    corrupted["roles"]["test"][0] = corrupted["roles"]["development"][0]
    with pytest.raises(ValueError, match="중복"):
        validate_patient_split(corrupted)
    corrupted = copy.deepcopy(split)
    corrupted["manifest_sha256"] = "untrusted"
    with pytest.raises(ValueError, match="해시"):
        validate_patient_split(corrupted)


def _write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def test_csv_preserves_original_semantics_uuid_dates_and_all_encounters(tmp_path: Path) -> None:
    spec = load_extract_spec(ROOT / "research/fhir_profile/data/specs/synthea.json")
    headers = ["DATE", "PATIENT", "ENCOUNTER", "CATEGORY", "CODE", "DESCRIPTION", "VALUE", "UNITS", "TYPE", "EXTRA"]
    _write_csv(tmp_path / "observations.csv", headers, [
        ["2020-01-02T10:00:00-05:00", "uuid-p", "enc-1", "laboratory", "718-7", "Hb", "12.30", "g/dL", "numeric", "keep"],
        ["2020-03-02", "uuid-p", "enc-2", "survey", "72166-2", "Smoking", "Never smoker", "", "text", "keep"],
        ["2020-03-03", "other", "enc-3", "survey", "72166-2", "Smoking", "Never smoker", "", "text", "keep"],
    ])
    batch = CSVRecordExtractor(tmp_path, spec).extract("Observation", ["uuid-p"])
    assert len(batch.records) == 2
    assert batch.records[0]["value"] == "12.30"
    assert batch.records[0]["date"] == "2020-01-02T10:00:00-05:00"
    assert batch.records[1]["type"] == "text"
    assert batch.records[1]["units"] == ""
    assert {row["encounter"] for row in batch.records} == {"enc-1", "enc-2"}
    assert batch.records[0]["extra"] == "keep"
    assert batch.records[0]["_source_record_id"] == "observations.csv:2"
    assert batch.metadata["column_normalization"]["TYPE"] == "type"
    assert batch.extraction["total_source_rows"] == 3
    assert batch.extraction["truncated"] is False
    assert len(batch.extraction["source_sha256"]) == 64
    assert batch.dataset_version == "unspecified"


def test_csv_rejects_duplicate_patients_and_missing_type(tmp_path: Path) -> None:
    spec = load_extract_spec(ROOT / "research/fhir_profile/data/specs/synthea.json")
    _write_csv(tmp_path / "patients.csv", ["Id", "BIRTHDATE", "DEATHDATE", "GENDER"], [
        ["a", "2000-01-01", "", "M"], ["a", "2000-01-01", "", "M"],
    ])
    with pytest.raises(ValueError, match="중복 환자"):
        CSVRecordExtractor(tmp_path, spec).extract("Patient")
    _write_csv(tmp_path / "observations.csv", ["PATIENT"], [["a"]])
    with pytest.raises(ValueError, match="필수 컬럼"):
        CSVRecordExtractor(tmp_path, spec).extract("Observation")


def test_database_extractor_supports_fixed_cohort_and_reports_truncation() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE results (id INTEGER, patient INTEGER, encounter INTEGER, value TEXT)"))
        connection.execute(text("INSERT INTO results VALUES (1, 10, 20, 'a'), (2, 10, 21, 'b'), (3, 11, 22, 'c')"))
    spec = {
        "dataset": "example", "dataset_version": "v1", "schema_version": "v1",
        "resources": {"Observation": {
            "source_resource": "results", "table": "results", "columns": ["id", "patient", "encounter", "value"],
            "primary_keys": ["id"], "patient_column": "patient", "encounter_column": "encounter",
        }},
    }
    extractor = PredefinedQueryExtractor(engine, spec)
    batch = extractor.extract("Observation", patient_keys=[10, 11])
    assert len(batch.records) == 3
    assert batch.extraction["expected_count"] == 3
    assert batch.extraction["truncated"] is False
    assert extractor.extract("Observation", patient_keys=[]).records == ()
    assert extractor.extract("Observation", 10, []).records == ()
    spec["resources"]["Observation"]["limit"] = 1
    limited = extractor.extract("Observation", 10)
    assert limited.extraction["expected_count"] == 2
    assert limited.extraction["truncated"] is True
    assert limited.extraction["limit_reached"] is True


def test_research_specs_do_not_silently_limit_patients_events() -> None:
    for dataset in ("mimic_iv", "synthea"):
        spec = load_extract_spec(ROOT / f"research/fhir_profile/data/specs/{dataset}.json")
        assert all("limit" not in resource for resource in spec["resources"].values())
