"""SQL Planning 없는 Profile 연구 CLI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine

from .artifacts import ROOT, freeze_policy, load_experiment_config, read_json, verify_policy, write_json
from .cohorts import create_cohort_lock, verify_locked_cohort
from .contracts import SourceBatch
from .data.extractors import CSVRecordExtractor, PredefinedQueryExtractor, load_extract_spec
from .experiment import evaluate_experiment, generate_profiles, prepare_cohort, save_evaluation
from .pipeline import HybridFhirPipeline
from .profiles.schema import load_profile
from .reference import load_reference_resources
from .runtime import FhirRetriever, ResearchOllamaClient, build_fhir_index
from .splits import create_fixed_patient_split, create_patient_split
from .analysis import compare_results


def _new_output(path: str) -> None:
    if Path(path).exists():
        raise ValueError(f"기존 산출물을 덮어쓸 수 없습니다: {path}")


def _runtime(lock_path: str, index_path: str | None, source_index_path: str | None = None):
    policy = verify_policy(read_json(lock_path))
    client = ResearchOllamaClient(policy["config"])
    retriever = None
    if policy["config"]["rag"].get("enabled", True):
        if not index_path:
            raise ValueError("RAG가 활성화된 잠금 정책에는 --index가 필요합니다.")
        retriever = FhirRetriever(index_path, client, top_k=int(policy["config"]["rag"].get("top_k", 6)))
    source_retriever = None
    if source_index_path:
        from .source_rag import SourceRetriever
        source_retriever = SourceRetriever(source_index_path, client,
                                           top_k=int(policy["config"]["rag"].get("source_top_k", 6)))
    return policy, client, retriever, source_retriever


def _evaluation_inputs(args, cohort, config):
    options = config.get("evaluation", {}).get("reference_by_dataset", {}).get(cohort["dataset"], {})
    reference = load_reference_resources(
        args.reference, patient_keys=cohort["patient_keys"], resource_types=list(cohort["resources"]), **options,
    ) if args.reference else None
    profiles = read_json(args.reference_profiles) if args.reference_profiles else None
    return {"reference_resources": reference, "reference_profiles": profiles}


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"--profile", "--records", "--correct"}:
        arguments.insert(0, "render")
    parser = argparse.ArgumentParser(description="MIMIC 프롬프트 개발 / Synthea 합성 외부 검증 (SQL Planning 없음)")
    commands = parser.add_subparsers(dest="command", required=True)
    split = commands.add_parser("split", help="환자 단위 개발/평가 분할")
    patient_source = split.add_mutually_exclusive_group(required=True)
    patient_source.add_argument("--patients", help="JSON 환자 key 배열")
    patient_source.add_argument("--fixed-roles", help="역할별로 지정한 환자 key 배열의 JSON 객체")
    split.add_argument("--dataset", choices=["mimic_iv", "synthea"], required=True)
    split.add_argument("--seed", type=int, default=42)
    split.add_argument("--output", required=True)
    prepare = commands.add_parser("prepare", help="고정 명세로 원천 행 추출")
    prepare.add_argument("--spec", required=True)
    prepare.add_argument("--split", required=True)
    prepare.add_argument("--role", required=True)
    source = prepare.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv-dir")
    source.add_argument("--db-url-env", help="DB URL을 가진 환경변수 이름")
    prepare.add_argument("--output", required=True)
    index = commands.add_parser("build-rag", help="FHIR Schema 전용 Vector index 생성")
    index.add_argument("--config", required=True)
    index.add_argument("--output", required=True)
    source_index = commands.add_parser("build-source-rag", help="허용된 추출 명세에서 원천 스키마 Vector index 생성")
    source_index.add_argument("--config", required=True)
    source_index.add_argument("--spec", required=True)
    source_index.add_argument("--output", required=True)
    baseline = commands.add_parser("baseline-configs", help="원천 RAG만 다른 스키마 전용 대응 설정 생성")
    baseline.add_argument("--config", required=True, help="동일 정책의 기반 실험 JSON")
    baseline.add_argument("--fhir-constraints", required=True, help="로컬 공식 FHIR R4 core package")
    baseline.add_argument("--output-dir", required=True)
    freeze = commands.add_parser("freeze", help="개발 완료 정책과 모델/코드/프롬프트 고정")
    freeze.add_argument("--config", required=True)
    freeze.add_argument("--index")
    freeze.add_argument("--output", required=True)
    compare = commands.add_parser("compare-results", help="환자 단위 대응 bootstrap 및 RAG ablation 비교")
    compare.add_argument("--left", required=True)
    compare.add_argument("--right", required=True)
    ablation = compare.add_mutually_exclusive_group()
    ablation.add_argument("--rag-ablation", action="store_true")
    ablation.add_argument("--source-rag-ablation", action="store_true")
    compare.add_argument("--seed", type=int, default=42)
    compare.add_argument("--resamples", type=int, default=2000)
    compare.add_argument("--output", required=True)
    for command, help_text in (
        ("cohort-lock", "준비·평가 원천 행과 독립 참조를 고정"),
        ("cohort-verify", "재사용할 고정 코호트가 원본과 일치하는지 확인"),
    ):
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument("--preparation", required=True)
        sub.add_argument("--records", required=True)
        sub.add_argument("--config", required=True)
        sub.add_argument("--reference")
        sub.add_argument("--reference-profiles")
        if command == "cohort-lock":
            sub.add_argument("--output", required=True)
        else:
            sub.add_argument("--cohort-lock", required=True)
    for command in ("generate-profile", "evaluate"):
        sub = commands.add_parser(command)
        sub.add_argument("--lock", required=True)
        sub.add_argument("--index")
        sub.add_argument("--source-index")
        sub.add_argument("--source-spec")
        sub.add_argument("--records", required=True)
        sub.add_argument("--cohort-lock", help="고정 코호트에서만 생성·평가하도록 강제")
        if command == "generate-profile":
            sub.add_argument("--output", required=True)
        else:
            sub.add_argument("--profiles", required=True)
            sub.add_argument("--reference", help="평가 전용 FHIR JSON/NDJSON 파일 또는 디렉터리")
            sub.add_argument("--reference-profiles", help="평가 전용 독립 매핑 Profile JSON (resourceType별 객체)")
            sub.add_argument("--groups", nargs="+", choices=["B1", "P1", "P2"], default=["B1", "P1", "P2"])
            sub.add_argument("--output-dir", required=True)
    render = commands.add_parser("render", help="단일 Profile 실행 (비교 실험 아님)")
    render.add_argument("--profile", required=True)
    render.add_argument("--records", required=True)
    render.add_argument("--correct", action="store_true")
    args = parser.parse_args(arguments)
    load_dotenv(ROOT / ".env", override=False)
    if hasattr(args, "output"):
        _new_output(args.output)
    if args.command == "compare-results":
        result = compare_results(read_json(args.left), read_json(args.right), seed=args.seed,
                                 resamples=args.resamples, rag_ablation=args.rag_ablation,
                                 source_rag_ablation=args.source_rag_ablation)
        write_json(args.output, result)
    elif args.command == "baseline-configs":
        from .artifacts import exact_fhir_constraints, validate_experiment_config
        base = read_json(args.config)
        base["generation"]["sample_rows"] = 0
        base["generation"]["managed_identity"] = True
        rag = {**base["rag"], "enabled": True, "source_contract": True,
               "fhir_constraints_path": str(Path(args.fhir_constraints).resolve())}
        output = Path(args.output_dir)
        configurations = {
            "fhir-only": {**base, "rag": {**rag, "source_enabled": False}},
            "dual": {**base, "rag": {**rag, "source_enabled": True}},
        }
        for mode, config in configurations.items():
            _new_output(str(output / f"{mode}.config.json"))
            validate_experiment_config(config)
        constraints = exact_fhir_constraints(configurations["dual"])
        for resource_type in base.get("resources", []):
            constraints.context(resource_type)
        for mode, config in configurations.items():
            write_json(output / f"{mode}.config.json", config)
        result = {}
    elif args.command == "split":
        if args.fixed_roles:
            result = create_fixed_patient_split(read_json(args.fixed_roles), args.dataset)
        else:
            result = create_patient_split(read_json(args.patients), args.dataset, seed=args.seed)
        write_json(args.output, result)
    elif args.command in {"cohort-lock", "cohort-verify"}:
        preparation = read_json(args.preparation)
        evaluation = read_json(args.records)
        inputs = _evaluation_inputs(args, evaluation, load_experiment_config(args.config))
        if args.command == "cohort-lock":
            result = create_cohort_lock(preparation, evaluation, **inputs)
            write_json(args.output, result)
        else:
            result = read_json(args.cohort_lock)
            verify_locked_cohort(result, preparation, "preparation")
            verify_locked_cohort(result, evaluation, "evaluation", **inputs)
    elif args.command == "prepare":
        spec = load_extract_spec(args.spec)
        engine = None
        if args.csv_dir:
            extractor = CSVRecordExtractor(args.csv_dir, spec)
        else:
            url = os.getenv(args.db_url_env, "").strip()
            if not url:
                raise ValueError(f"환경변수 {args.db_url_env}에 DB URL이 필요합니다.")
            engine = create_engine(url)
            extractor = PredefinedQueryExtractor(engine, spec)
        try:
            result = prepare_cohort(spec, read_json(args.split), args.role, extractor)
        finally:
            if engine:
                engine.dispose()
        write_json(args.output, result)
    elif args.command == "build-rag":
        config = load_experiment_config(args.config)
        client = ResearchOllamaClient(config)
        result = build_fhir_index(config, client)
        write_json(args.output, result)
    elif args.command == "build-source-rag":
        from .source_rag import build_source_index
        config = load_experiment_config(args.config)
        if not config["rag"].get("source_enabled"):
            raise ValueError("원천 RAG를 활성화한 설정이 필요합니다.")
        result = build_source_index(load_extract_spec(args.spec), config, ResearchOllamaClient(config))
        write_json(args.output, result)
    elif args.command == "freeze":
        config = load_experiment_config(args.config)
        client = ResearchOllamaClient(config)
        identity = {"enabled": False}
        if config["rag"].get("enabled", True):
            if not args.index:
                raise ValueError("RAG 정책 잠금에는 --index가 필요합니다.")
            retriever = FhirRetriever(args.index, client)
            identity = retriever.identity
            if client.identity(client.embedding_model) != identity["embedding_identity"]:
                raise ValueError("Vector index의 임베딩 모델이 현재 모델과 다릅니다.")
        result = freeze_policy(config, model_identity=client.identity(), rag_identity=identity)
        if config["rag"].get("enabled", True) and result["fhir_schema_sha256"] != identity["schema_sha256"]:
            raise ValueError("Vector index의 FHIR Schema 버전이 다릅니다.")
        write_json(args.output, result)
    elif args.command == "generate-profile":
        cohort = read_json(args.records)
        if args.cohort_lock:
            verify_locked_cohort(read_json(args.cohort_lock), cohort, "preparation")
        policy, client, retriever, source_retriever = _runtime(args.lock, args.index, args.source_index)
        source_spec = load_extract_spec(args.source_spec) if args.source_spec else None
        def checkpoint(event):
            destination = Path(args.output).with_suffix(".checkpoints")
            write_json(destination / f"{event['stage']}-{event['resource_type']}.json", event)
            print(json.dumps({"stage": event["stage"], "resource_type": event["resource_type"]}), file=sys.stderr, flush=True)
        result = generate_profiles(policy, cohort, client, retriever, on_checkpoint=checkpoint,
                                   source_spec=source_spec, source_retriever=source_retriever)
        write_json(args.output, result)
    elif args.command == "evaluate":
        destination = Path(args.output_dir)
        if destination.exists() and any(destination.iterdir()):
            raise ValueError("평가 결과 디렉터리는 비어 있어야 합니다.")
        policy, client, retriever, source_retriever = _runtime(args.lock, args.index, args.source_index)
        source_spec = load_extract_spec(args.source_spec) if args.source_spec else None
        cohort = read_json(args.records)
        profiles = read_json(args.profiles)
        inputs = _evaluation_inputs(args, cohort, policy["config"])
        cohort_lock = read_json(args.cohort_lock) if args.cohort_lock else None
        def checkpoint(event):
            checkpoint_directory = destination.with_name(destination.name + ".checkpoints")
            write_json(checkpoint_directory / f"{event['group']}.json", event)
            print(json.dumps({"stage": event["stage"], "group": event["group"]}), file=sys.stderr, flush=True)
        result = evaluate_experiment(policy, cohort, profiles, client, retriever, groups=args.groups,
                                     on_checkpoint=checkpoint, cohort_lock=cohort_lock,
                                     source_spec=source_spec, source_retriever=source_retriever, **inputs)
        save_evaluation(result, args.output_dir)
    else:
        if args.correct:
            parser.error("LLM Profile 교정은 잠금 정책을 지정한 generate-profile 명령에서 수행합니다.")
        result = HybridFhirPipeline(correct=args.correct).run(SourceBatch.from_dict(read_json(args.records)), load_profile(args.profile))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    print(json.dumps({"command": args.command, "output": getattr(args, "output", getattr(args, "output_dir", None)),
                      "policy_sha256": result.get("policy_sha256"), "cohort_lock_sha256": result.get("cohort_lock_sha256"),
                      "dataset": result.get("dataset")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
