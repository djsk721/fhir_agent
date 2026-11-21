"""프로젝트 루트에서 실행하기 위한 얇은 CLI 진입점."""

import sys


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "research":
        from research.fhir_profile.__main__ import main
        main(sys.argv[2:])
    else:
        from fhir_agent_pipeline.cli import entrypoint
        entrypoint()
