"use client";

import { useMemo, useState } from "react";

import {
  getApiBaseUrl,
  normalizeAnalyzeInput,
  postAnalyze,
  postMapping,
  type AnalyzeRequest,
  type AnalyzeResponse,
  type DebugContext,
  type MappingRequest,
  type MappingResponse,
} from "@/api";
import { DebugPanel } from "@/components/DebugPanel";
import { InputPanel } from "@/components/InputPanel";
import { MappingPanel } from "@/components/MappingPanel";
import { OutputViewer } from "@/components/OutputViewer";
import { DEFAULT_PAYLOAD } from "@/lib/samplePayload";

type StatusTone = "idle" | "loading" | "success" | "error";
type RequestKind = "mapping" | "analyze" | null;

function parseResourceTypes(input: string): string[] | undefined {
  const resourceTypes = input
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);

  return resourceTypes.length > 0 ? resourceTypes : undefined;
}

function parsePayloadText(payloadText: string): unknown {
  const trimmed = payloadText.trim();

  if (!trimmed) {
    throw new Error("Payload JSON is empty. Paste a valid request body before running mapping or analyze.");
  }

  try {
    return JSON.parse(trimmed) as unknown;
  } catch {
    throw new Error("Payload JSON is invalid. Check for a missing brace, quote, or comma and try again.");
  }
}

function parseSubjectIdOverride(input: string): number | undefined {
  const trimmed = input.trim();

  if (!trimmed) {
    return undefined;
  }

  if (!/^\d+$/.test(trimmed)) {
    throw new Error("Subject ID must be a positive whole number when the override field is used.");
  }

  const subjectId = Number(trimmed);

  if (!Number.isSafeInteger(subjectId) || subjectId <= 0) {
    throw new Error("Subject ID must be a positive whole number when the override field is used.");
  }

  return subjectId;
}

function applySubjectIdOverride(request: AnalyzeRequest, subjectIdInput: string): AnalyzeRequest {
  const subjectId = parseSubjectIdOverride(subjectIdInput);

  if (subjectId === undefined) {
    return request;
  }

  const patient = request.payload.patient;

  if (!patient || typeof patient !== "object" || Array.isArray(patient)) {
    throw new Error("Subject ID override requires payload.patient to be an object in the JSON input.");
  }

  return {
    ...request,
    payload: {
      ...request.payload,
      patient: {
        ...patient,
        subject_id: subjectId,
      },
    },
  };
}

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
    const parsed = parsePayloadText(payloadText);
    const request = normalizeAnalyzeInput(parsed);
    const resourceTypes = parseResourceTypes(resourceTypesText) ?? request.resource_types;

    return applySubjectIdOverride({
      ...request,
      resource_types: resourceTypes,
    }, subjectIdOverride);
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
      const request: MappingRequest = {
        payload: analyzeRequest.payload,
      };

      if (mappingItemId.trim()) {
        request.itemid = mappingItemId.trim();
      }

      const response = await postMapping(request);
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
              <dd>{`{ payload, resource_types? }`}</dd>
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

      <section className="content-grid">
        <OutputViewer result={analyzeResult} />
        <MappingPanel result={mappingResult} />
        <DebugPanel debugContext={debugContext} lastAction={lastAction} />
      </section>
    </main>
  );
}
