import type { MappingResponse } from "@/lib/api";
import { JsonBlock } from "@/components/JsonBlock";
import { Panel } from "@/components/Panel";

type MappingPanelProps = {
  result: MappingResponse | null;
};

export function MappingPanel({ result }: MappingPanelProps) {
  return (
    <Panel
      title="Mapping explanation"
      kicker="Mapping"
      description="Review the backend's explanation and the normalized payload used to drive downstream FHIR generation."
    >
      <div className="json-stack">
        <div className="json-section">
          <div className="json-section-header">
            <h3>Explanation</h3>
            {result?.itemid !== undefined && result?.itemid !== null ? <span className="pill">itemid {String(result.itemid)}</span> : null}
          </div>
          <pre className="json-block">
            {result?.mapping_explanation || "Run mapping to inspect the backend explanation for the current payload."}
          </pre>
        </div>

        <JsonBlock
          title="Mapping payload"
          value={result?.mapping_payload}
          emptyMessage="Mapping payload output will appear here after a successful mapping request."
        />
      </div>
    </Panel>
  );
}
