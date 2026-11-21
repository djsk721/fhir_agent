#!/usr/bin/env bash
# Run a matched, schema-only FHIR-only vs dual-RAG pilot for one fixed cohort.
set -euo pipefail
umask 077

usage() {
    printf '%s\n' \
        'Usage (from the repository root):' \
        '  bash experiments/run_dual_rag_baseline.sh \' \
        '    --fhir-only-lock PATH --dual-lock PATH --dual-config PATH \' \
        '    --index PATH --source-spec PATH [--source-index PATH] \' \
        '    --preparation PATH --test PATH --cohort-lock PATH \' \
        '    --reference PATH --reference-profiles PATH --output PATH' \
        '' \
        'Reuse the same two policy locks and FHIR index for each dataset.' \
        'Omit --source-index to build a new source index from that dataset’s extract spec.' \
        'The output directory must not exist; its parent must already exist.' \
        'Only P1 and P2 are run. References are loaded by cohort verification/evaluation, not Profile generation.' \
        'Keep the output (including logs) local; validation errors can contain resource identifiers.'
}

die() {
    printf 'baseline: %s\n' "$*" >&2
    exit 2
}

declare -A opts=()
while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --fhir-only-lock|--dual-lock|--dual-config|--index|--source-spec|--source-index|--preparation|--test|--cohort-lock|--reference|--reference-profiles|--output)
            (($# >= 2)) || die "missing value for $1"
            [[ -n "$2" && "$2" != --* ]] || die "missing value for $1"
            [[ ! -v 'opts[$1]' ]] || die "duplicate option: $1"
            opts[$1]=$2
            shift 2
            ;;
        *) die "unknown option: $1" ;;
    esac
done

required_files=(--fhir-only-lock --dual-lock --dual-config --index --source-spec
                --preparation --test --cohort-lock --reference-profiles)
for option in "${required_files[@]}"; do
    path=${opts[$option]:-}
    [[ -n "$path" ]] || die "required option: $option"
    [[ -f "$path" && -r "$path" ]] || die "not a readable file for $option: $path"
done
reference=${opts[--reference]:-}
[[ -n "$reference" ]] || die 'required option: --reference'
[[ -r "$reference" && ( -f "$reference" || -d "$reference" ) ]] || die "not a readable reference input: $reference"
out=${opts[--output]:-}
[[ -n "$out" ]] || die 'required option: --output'
[[ -f main.py && -d research/fhir_profile ]] || die 'run from the repository root'

source_index=${opts[--source-index]:-}
if [[ -n "$source_index" ]]; then
    [[ -f "$source_index" && -r "$source_index" ]] || die "not a readable source index: $source_index"
fi
python_bin=${FHIR_RESEARCH_PYTHON:-python}
command -v "$python_bin" >/dev/null || die "Python executable not found: $python_bin"
mkdir -m 700 -- "$out" || die "cannot create a new output directory: $out"

run() {
    "$python_bin" main.py research "$@" 2>&1 | tee -a "$out/run.log"
}

evaluation_inputs=(--reference "$reference" --reference-profiles "${opts[--reference-profiles]}")
run cohort-verify --config "${opts[--dual-config]}" --cohort-lock "${opts[--cohort-lock]}" \
    --preparation "${opts[--preparation]}" --records "${opts[--test]}" "${evaluation_inputs[@]}"

if [[ -z "$source_index" ]]; then
    source_index="$out/source-index.json"
    run build-source-rag --config "${opts[--dual-config]}" --spec "${opts[--source-spec]}" --output "$source_index"
fi

for arm in fhir-only dual; do
    arm_options=(--lock "${opts[--$arm-lock]}" --index "${opts[--index]}"
                 --source-spec "${opts[--source-spec]}" --cohort-lock "${opts[--cohort-lock]}")
    if [[ "$arm" == dual ]]; then
        arm_options+=(--source-index "$source_index")
    fi
    run generate-profile "${arm_options[@]}" --records "${opts[--preparation]}" --output "$out/$arm.profiles.json"
    run evaluate "${arm_options[@]}" --records "${opts[--test]}" --profiles "$out/$arm.profiles.json" \
        "${evaluation_inputs[@]}" --groups P1 P2 --output-dir "$out/$arm"
done

run compare-results --source-rag-ablation \
    --left "$out/fhir-only/manifest.json" --right "$out/dual/manifest.json" \
    --output "$out/source-rag-comparison.json"
