"use client";

import { useMemo, useState } from "react";

import {
  getApiBaseUrl,
  postAnalyze,
  postMapping,
  type AnalyzeResponse,
  type DebugContext,
  type MappingResponse,
} from "@/lib/api";
import { DebugPanel } from "@/components/DebugPanel";
import { InputPanel } from "@/components/InputPanel";
import { MappingPanel } from "@/components/MappingPanel";
import { OutputViewer } from "@/components/OutputViewer";
import { buildAnalyzeRequest, buildMappingRequest } from "@/lib/request-utils";
import { DEFAULT_PAYLOAD } from "@/lib/samplePayload";

type StatusTone = "idle" | "loading" | "success" | "error";
type RequestKind = "mapping" | "analyze" | null;

export default function HomePage() {
  const apiBaseUrl = useMemo(() => getApiBaseUrl(), []);
  const [payloadText, setPayloadText] = useState(DEFAULT_PAYLOAD);
  const [resourceTypesText, setResourceTypesText] = useState("");
  const [subjectIdOverride, setSubjectIdOverride] = useState("");
  const [mappingItemId, setMappingItemId] = useState("");
  const [statusMessage, setStatusMessage] = useState(`Using backend at ${apiBaseUrl}`);
  const [statusTone, setStatusTone] = useState<StatusTone>("idle");
  const [busyAction, setBusyAction] = useState<RequestKind>(null);
  const [lastAction, setLastAction] = useState<RequestKind>(null);
  const [mappingResult, setMappingResult] = useState<MappingResponse | null>(null);
  const [analyzeResult, setAnalyzeResult] = useState<AnalyzeResponse | null>(null);
  const [debugContext, setDebugContext] = useState<DebugContext | null>(null);

  const handleError = (message: string) => {
    setStatusTone("error");
    setStatusMessage(message);
  };

  const parseAnalyzeRequest = () => {
    return buildAnalyzeRequest(payloadText, resourceTypesText, subjectIdOverride);
  };

  const runAnalyze = async () => {
    setBusyAction("analyze");
    setStatusTone("loading");
    setStatusMessage("Running analysis against the FastAPI backend...");

    try {
      const request = parseAnalyzeRequest();
      const response = await postAnalyze(request);
      setAnalyzeResult(response);
      setDebugContext(response.debug_context);
      setLastAction("analyze");
      setStatusTone("success");
      setStatusMessage("Analyze completed. FHIR bundle and debug context refreshed.");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unexpected analyze request failure.";
      handleError(message);
    } finally {
      setBusyAction(null);
    }
  };

  const runMapping = async () => {
    setBusyAction("mapping");
    setStatusTone("loading");
    setStatusMessage("Running mapping explanation against the FastAPI backend...");

    try {
      const analyzeRequest = parseAnalyzeRequest();
      const response = await postMapping(buildMappingRequest(analyzeRequest, mappingItemId));
      setMappingResult(response);
      setDebugContext(response.debug_context);
      setLastAction("mapping");
      setStatusTone("success");
      setStatusMessage("Mapping completed. Explanation and debug context refreshed.");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unexpected mapping request failure.";
      handleError(message);
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <main className="page-shell">
      <header className="page-header">
        <p className="eyebrow">FHIR Agent Service</p>
        <h1>Analyze MIMIC payloads and inspect FHIR generation from a real Next.js frontend.</h1>
        <p>
          This App Router frontend talks directly to the FastAPI backend on port 8000 by default, with an override via
          <code> NEXT_PUBLIC_API_BASE_URL</code> when needed.
        </p>
      </header>

      <section className="hero-grid">
        <InputPanel
          payloadText={payloadText}
          resourceTypesText={resourceTypesText}
          subjectIdOverride={subjectIdOverride}
          mappingItemId={mappingItemId}
          apiBaseUrl={apiBaseUrl}
          statusMessage={statusMessage}
          statusTone={statusTone}
          busyAction={busyAction}
          onPayloadChange={setPayloadText}
          onResourceTypesChange={setResourceTypesText}
          onSubjectIdOverrideChange={setSubjectIdOverride}
          onMappingItemIdChange={setMappingItemId}
          onRunMapping={runMapping}
          onRunAnalyze={runAnalyze}
        />

        <aside className="panel meta-card">
          <header className="panel-header">
            <p className="panel-kicker">Connection</p>
            <h2 className="panel-title">Frontend + backend handshake</h2>
            <p className="panel-description">
              The browser app targets a local FastAPI server without code changes, so <code>npm run dev</code> works right away.
            </p>
          </header>

          <dl className="meta-list">
            <div className="meta-row">
              <dt>Frontend URL</dt>
              <dd>http://localhost:3000</dd>
            </div>
            <div className="meta-row">
              <dt>Default backend URL</dt>
              <dd>{apiBaseUrl}</dd>
            </div>
            <div className="meta-row">
              <dt>Analyze request shape</dt>
              <dd>{`{ payload, resource_types?, enable_cross_validation? }`}</dd>
            </div>
            <div className="meta-row">
              <dt>Mapping request shape</dt>
              <dd>{`{ itemid?, payload?, notes? }`}</dd>
            </div>
            <div className="meta-row">
              <dt>Expected backend routes</dt>
              <dd>POST /analyze and POST /mapping</dd>
            </div>
          </dl>
        </aside>
      </section>

      <section className="results-workspace">
        <div className="results-primary">
          <OutputViewer result={analyzeResult} />
        </div>

        <div className="results-secondary-grid">
          <MappingPanel result={mappingResult} />
          <DebugPanel debugContext={debugContext} lastAction={lastAction} />
        </div>
      </section>
    </main>
  );
}
