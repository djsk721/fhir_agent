import type { DebugContext } from "@/api";
import { JsonBlock } from "@/components/JsonBlock";
import { Panel } from "@/components/Panel";

type DebugPanelProps = {
  debugContext: DebugContext | null;
  lastAction: "mapping" | "analyze" | null;
};

export function DebugPanel({ debugContext, lastAction }: DebugPanelProps) {
  return (
    <Panel
      title="Debug panel"
      kicker="RAG / Debug"
      description="Track agent order, retrieval context, schema context, and payload summary from the latest backend response."
      compact
      actions={lastAction ? <span className="pill">Last run: {lastAction}</span> : undefined}
    >
      {debugContext ? (
        <div className="json-stack">
          <div className="debug-list">
            <div className="debug-item">
              <div className="json-section-header">
                <h3>Agent sequence</h3>
                <span className="pill">{debugContext.agent_sequence.length} steps</span>
              </div>
              <pre className="json-block">{JSON.stringify(debugContext.agent_sequence, null, 2)}</pre>
            </div>

            <div className="debug-item">
              <div className="json-section-header">
                <h3>Warnings</h3>
                <span className="pill">{debugContext.warnings.length}</span>
              </div>
              {debugContext.warnings.length > 0 ? (
                <ul className="warning-list">
                  {debugContext.warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              ) : (
                <p className="placeholder-text">No warnings were returned in the latest debug context.</p>
              )}
            </div>
          </div>

          <JsonBlock
            title="RAG context"
            value={debugContext.rag_context}
            emptyMessage="The backend did not return retrieval context for the latest request."
          />

          <JsonBlock
            title="Schema context"
            value={debugContext.schema_context}
            emptyMessage="Schema context will appear here when supplied by the backend."
          />

          <JsonBlock
            title="Source payload summary"
            value={debugContext.source_payload_summary}
            emptyMessage="Source payload summary will appear here when supplied by the backend."
          />
        </div>
      ) : (
        <p className="placeholder-text">Run mapping or analyze to populate the debug panel with RAG and schema details.</p>
      )}
    </Panel>
  );
}
