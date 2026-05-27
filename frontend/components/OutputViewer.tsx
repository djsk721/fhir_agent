import type { AnalyzeCorrection, AnalyzeResponse, CrossValidationItem } from "@/lib/api";
import { JsonBlock } from "@/components/JsonBlock";
import { Panel } from "@/components/Panel";

type OutputViewerProps = {
  result: AnalyzeResponse | null;
};

type Tone = "success" | "error" | "warning" | "neutral";

function humanizeLabel(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function getTone(status: string | null | undefined): Tone {
  const normalized = status?.toLowerCase();

  if (["matched", "passed", "success", "ok", "valid"].includes(normalized ?? "")) {
    return "success";
  }

  if (["mismatched", "error", "failed", "invalid"].includes(normalized ?? "")) {
    return "error";
  }

  if (["missing", "partial", "warning"].includes(normalized ?? "")) {
    return "warning";
  }

  return "neutral";
}

function hasStringEntries(values: unknown[]): values is string[] {
  return values.every((value) => typeof value === "string");
}

function formatReferenceLocation(item: CrossValidationItem): string | null {
  if (!item.reference_file) {
    return null;
  }

  if (item.reference_line === null || item.reference_line === undefined) {
    return item.reference_file;
  }

  return `${item.reference_file}:${item.reference_line}`;
}

function renderListOrJson(title: string, values: unknown[], emptyMessage: string) {
  if (values.length === 0) {
    return <p className="placeholder-text">{emptyMessage}</p>;
  }

  if (hasStringEntries(values)) {
    return (
      <div className="json-section">
        <div className="json-section-header">
          <h4>{title}</h4>
          <span className="pill">{values.length}</span>
        </div>
        <ul className="detail-list">
          {values.map((value) => (
            <li key={value}>{value}</li>
          ))}
        </ul>
      </div>
    );
  }

  return <JsonBlock title={title} value={values} emptyMessage={emptyMessage} compact headingLevel="h4" />;
}

function renderCorrections(corrections: AnalyzeCorrection[]) {
  if (corrections.length === 0) {
    return null;
  }

  if (hasStringEntries(corrections)) {
    return (
      <div className="json-section">
        <div className="json-section-header">
          <h3>Corrections</h3>
          <span className="pill">{corrections.length}</span>
        </div>
        <ul className="detail-list">
          {corrections.map((correction) => (
            <li key={correction}>{correction}</li>
          ))}
        </ul>
      </div>
    );
  }

  return (
    <JsonBlock
      title="Corrections"
      value={corrections}
      emptyMessage="Correction details will appear here when the backend returns remediation guidance."
      compact
    />
  );
}

export function OutputViewer({ result }: OutputViewerProps) {
  const crossValidation = result?.cross_validation ?? null;
  const corrections = result?.corrections ?? [];
  const verificationErrors = result?.verification_errors ?? [];

  return (
    <Panel
      title="FHIR output viewer"
      kicker="Analyze"
      description="Scan cross-validation outcomes first, then inspect corrections, verification feedback, and the raw FHIR outputs returned by the analyze pipeline."
      actions={
        result ? (
          <div className="panel-action-group">
            <span className="pill">Attempts {result.attempts_used}</span>
            {crossValidation ? <span className="pill">Cross-validation {humanizeLabel(crossValidation.summary.status)}</span> : null}
          </div>
        ) : undefined
      }
    >
      <div className="json-stack">
        <div className="json-section">
          <div className="json-section-header">
            <h3>Cross-validation summary</h3>
            {crossValidation ? (
              <span className={`status-chip status-chip--${getTone(crossValidation.summary.status)}`}>
                {humanizeLabel(crossValidation.summary.status)}
              </span>
            ) : null}
          </div>

          {crossValidation ? (
            <div className="analysis-summary">
              <div className="summary-grid">
                <div className={`summary-card summary-card--${getTone(crossValidation.summary.status)}`}>
                  <span className="summary-label">Overall status</span>
                  <strong className="summary-value">{humanizeLabel(crossValidation.summary.status)}</strong>
                </div>
                <div className="summary-card">
                  <span className="summary-label">Matched</span>
                  <strong className="summary-value">{crossValidation.summary.matched}</strong>
                </div>
                <div className={`summary-card summary-card--${crossValidation.summary.mismatched > 0 ? "error" : "neutral"}`}>
                  <span className="summary-label">Mismatched</span>
                  <strong className="summary-value">{crossValidation.summary.mismatched}</strong>
                </div>
                <div className={`summary-card summary-card--${crossValidation.summary.missing > 0 ? "warning" : "neutral"}`}>
                  <span className="summary-label">Missing</span>
                  <strong className="summary-value">{crossValidation.summary.missing}</strong>
                </div>
                <div className="summary-card">
                  <span className="summary-label">Skipped</span>
                  <strong className="summary-value">{crossValidation.summary.skipped}</strong>
                </div>
              </div>

              {crossValidation.reference_directory ? (
                <p className="supporting-copy">
                  Reference directory: <code>{crossValidation.reference_directory}</code>
                </p>
              ) : null}
            </div>
          ) : (
            <p className="placeholder-text">
              Run analyze to compare generated resources against the reference directory and surface summary counts here.
            </p>
          )}
        </div>

        <div className="json-section">
          <div className="json-section-header">
            <h3>Cross-validation items</h3>
            {crossValidation ? <span className="pill">{crossValidation.items.length} items</span> : null}
          </div>

          {crossValidation ? (
            crossValidation.items.length > 0 ? (
              <div className="cross-validation-list">
                {crossValidation.items.map((item, index) => {
                  const referenceLocation = formatReferenceLocation(item);

                  return (
                    <article className="result-card" key={`${item.resource_type}-${item.generated_id ?? "unknown"}-${index}`}>
                      <div className="json-section-header">
                        <div>
                          <h4>{item.resource_type}</h4>
                          <p className="supporting-copy">
                            {item.generated_id ? `Generated id ${item.generated_id}` : "Generated id unavailable"}
                          </p>
                        </div>
                        <span className={`status-chip status-chip--${getTone(item.status)}`}>{humanizeLabel(item.status)}</span>
                      </div>

                      <dl className="meta-list cross-validation-meta">
                        {referenceLocation ? (
                          <div className="meta-row">
                            <dt>Reference location</dt>
                            <dd>{referenceLocation}</dd>
                          </div>
                        ) : null}

                        {item.skipped_reason ? (
                          <div className="meta-row">
                            <dt>Skipped reason</dt>
                            <dd>{item.skipped_reason}</dd>
                          </div>
                        ) : null}
                      </dl>

                      {renderListOrJson(
                        "Differences",
                        item.differences,
                        item.status === "matched"
                          ? "No differences were reported for this item."
                          : "No explicit difference details were returned for this item.",
                      )}

                      <div className="cross-validation-grid">
                        <JsonBlock
                          title="Generated resource"
                          value={item.generated_resource}
                          emptyMessage="No generated resource payload was returned for this item."
                          compact
                          headingLevel="h4"
                        />
                        <JsonBlock
                          title="Reference resource"
                          value={item.reference_resource}
                          emptyMessage="No reference resource payload was returned for this item."
                          compact
                          headingLevel="h4"
                        />
                      </div>
                    </article>
                  );
                })}
              </div>
            ) : (
              <p className="placeholder-text">Cross-validation completed, but the backend did not return any item-level details.</p>
            )
          ) : (
            <p className="placeholder-text">
              Item-by-item match status, differences, and reference details will appear here after analyze runs.
            </p>
          )}
        </div>

        {renderCorrections(corrections)}

        <div className="json-section">
          <div className="json-section-header">
            <h3>Verification errors</h3>
            {result ? (
              verificationErrors.length === 0 ? <span className="pill">No errors</span> : <span className="pill">{verificationErrors.length}</span>
            ) : null}
          </div>

          {result ? (
            verificationErrors.length > 0 ? (
              <ul className="detail-list">
                {verificationErrors.map((error) => (
                  <li key={error}>{error}</li>
                ))}
              </ul>
            ) : (
              <p className="placeholder-text">No verification errors were returned for the latest analyze response.</p>
            )
          ) : (
            <p className="placeholder-text">Any validation or verifier feedback will appear here after running analyze.</p>
          )}
        </div>

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
      </div>
    </Panel>
  );
}
