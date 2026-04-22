type InputPanelProps = {
  payloadText: string;
  resourceTypesText: string;
  subjectIdOverride: string;
  mappingItemId: string;
  apiBaseUrl: string;
  statusMessage: string;
  statusTone: "idle" | "loading" | "success" | "error";
  busyAction: "mapping" | "analyze" | null;
  onPayloadChange: (value: string) => void;
  onResourceTypesChange: (value: string) => void;
  onSubjectIdOverrideChange: (value: string) => void;
  onMappingItemIdChange: (value: string) => void;
  onRunMapping: () => void;
  onRunAnalyze: () => void;
};

export function InputPanel({
  payloadText,
  resourceTypesText,
  subjectIdOverride,
  mappingItemId,
  apiBaseUrl,
  statusMessage,
  statusTone,
  busyAction,
  onPayloadChange,
  onResourceTypesChange,
  onSubjectIdOverrideChange,
  onMappingItemIdChange,
  onRunMapping,
  onRunAnalyze,
}: InputPanelProps) {
  const statusClassName = [
    "status-banner",
    statusTone === "success" ? "status-banner--success" : "",
    statusTone === "error" ? "status-banner--error" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="panel">
      <header className="panel-header">
        <p className="panel-kicker">Input</p>
        <div className="panel-title-row">
          <h2 className="panel-title">MIMIC request payload</h2>
          <span className="pill">POST /analyze + /mapping</span>
        </div>
        <p className="panel-description">
          Paste either a full analyze request with a top-level <code>payload</code> field or a raw MIMIC payload object.
        </p>
      </header>

      <div className="field-group">
        <label className="field-label" htmlFor="payload-input">
          JSON input
        </label>
        <textarea
          id="payload-input"
          className="field-textarea"
          spellCheck={false}
          value={payloadText}
          onChange={(event) => onPayloadChange(event.target.value)}
        />
      </div>

      <div className="field-grid">
        <div className="field-group">
          <label className="field-label" htmlFor="resource-types-input">
            Analyze resource types
            <span className="pill">optional</span>
          </label>
          <input
            id="resource-types-input"
            className="field-input"
            placeholder="Patient, Encounter, Observation, Bundle"
            value={resourceTypesText}
            onChange={(event) => onResourceTypesChange(event.target.value)}
          />
          <p className="field-help">Comma-separated values are passed to the backend as <code>resource_types</code>.</p>
        </div>

        <div className="field-group">
          <label className="field-label" htmlFor="subject-id-override-input">
            Patient subject_id override
            <span className="pill">optional</span>
          </label>
          <input
            id="subject-id-override-input"
            className="field-input"
            inputMode="numeric"
            placeholder="10000032"
            value={subjectIdOverride}
            onChange={(event) => onSubjectIdOverrideChange(event.target.value)}
          />
          <p className="field-help">
            If set, analyze and mapping requests override <code>payload.patient.subject_id</code> without changing the JSON input.
          </p>
        </div>

        <div className="field-group">
          <label className="field-label" htmlFor="mapping-itemid-input">
            Mapping itemid override
            <span className="pill">optional</span>
          </label>
          <input
            id="mapping-itemid-input"
            className="field-input"
            placeholder="50868"
            value={mappingItemId}
            onChange={(event) => onMappingItemIdChange(event.target.value)}
          />
          <p className="field-help">If set, the mapping request uses this <code>itemid</code> while still sending payload context.</p>
        </div>
      </div>

      <div className="action-row">
        <button className="button button--secondary" type="button" onClick={onRunMapping} disabled={busyAction !== null}>
          {busyAction === "mapping" ? "Running mapping..." : "Run mapping"}
        </button>
        <button className="button" type="button" onClick={onRunAnalyze} disabled={busyAction !== null}>
          {busyAction === "analyze" ? "Running analysis..." : "Run analyze"}
        </button>
      </div>

      <div className={statusClassName}>
        <span className="status-label">
          {statusTone === "error"
            ? "Request error"
            : statusTone === "success"
              ? "Ready"
              : busyAction
                ? "Working"
                : "Backend target"}
        </span>
        <span>{statusMessage || apiBaseUrl}</span>
      </div>
    </div>
  );
}
