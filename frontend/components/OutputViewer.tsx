import type { AnalyzeResponse } from "@/api";
import { JsonBlock } from "@/components/JsonBlock";
import { Panel } from "@/components/Panel";

type OutputViewerProps = {
  result: AnalyzeResponse | null;
};

export function OutputViewer({ result }: OutputViewerProps) {
  return (
    <Panel
      title="FHIR output viewer"
      kicker="Analyze"
      description="Inspect the final FHIR bundle, generated resources, and validation feedback returned by the analyze pipeline."
      actions={result ? <span className="pill">Attempts {result.attempts_used}</span> : undefined}
    >
      <div className="json-stack">
        <JsonBlock
          title="FHIR bundle"
          value={result?.fhir_bundle}
          emptyMessage="Run analyze to render the generated FHIR bundle."
        />

        <JsonBlock
          title="Generated resources"
          value={result?.generated_resources}
          emptyMessage="Generated FHIR resource fragments will appear here."
        />

        <div className="json-section">
          <div className="json-section-header">
            <h3>Verification errors</h3>
            {result && result.verification_errors.length === 0 ? <span className="pill">No errors</span> : null}
          </div>
          <pre className="json-block">
            {result
              ? result.verification_errors.length > 0
                ? JSON.stringify(result.verification_errors, null, 2)
                : "[]"
              : "Any validation or verifier feedback will appear here after running analyze."}
          </pre>
        </div>
      </div>
    </Panel>
  );
}
