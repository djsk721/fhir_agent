export type JsonObject = Record<string, unknown>;

export type DebugContext = {
  agent_sequence: string[];
  rag_context: Array<Record<string, unknown>>;
  schema_context: Record<string, unknown>;
  source_payload_summary: Record<string, unknown>;
  warnings: string[];
};

export type MimicPayload = {
  patient: JsonObject;
  admission?: JsonObject | null;
  lab_event?: JsonObject | null;
};

export type AnalyzeRequest = {
  payload: MimicPayload;
  resource_types?: string[];
};

export type MappingRequest = {
  itemid?: number | string | null;
  payload?: MimicPayload | null;
  notes?: string | null;
};

export type AnalyzeResponse = {
  mapping_payload: JsonObject;
  generated_resources: Record<string, JsonObject>;
  fhir_bundle: JsonObject;
  verification_errors: string[];
  attempts_used: number;
  debug_context: DebugContext;
};

export type MappingResponse = {
  itemid?: number | string | null;
  mapping_payload: JsonObject;
  mapping_explanation: string;
  debug_context: DebugContext;
};

const DEFAULT_API_BASE_URL = "http://localhost:8000";

function trimTrailingSlash(value: string): string {
  return value.replace(/\/$/, "");
}

export function getApiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL;
  return trimTrailingSlash(configured || DEFAULT_API_BASE_URL);
}

function extractErrorMessage(data: unknown): string {
  if (typeof data === "string") {
    return data;
  }

  if (data && typeof data === "object" && "detail" in data) {
    const detail = (data as { detail?: unknown }).detail;
    if (typeof detail === "string") {
      return detail;
    }

    return JSON.stringify(detail, null, 2);
  }

  return "Request failed.";
}

function parseJsonText(text: string): unknown {
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  const text = await response.text();
  const data = text ? parseJsonText(text) : null;

  if (!response.ok) {
    throw new Error(extractErrorMessage(data));
  }

  return data as T;
}

export function normalizeAnalyzeInput(input: unknown): AnalyzeRequest {
  if (!input || typeof input !== "object") {
    throw new Error("Input JSON must be an object.");
  }

  if ("payload" in input) {
    const request = input as Partial<AnalyzeRequest>;
    if (!request.payload || typeof request.payload !== "object") {
      throw new Error("The 'payload' field must contain patient/admission/lab_event data.");
    }

    return {
      payload: request.payload,
      resource_types: request.resource_types,
    };
  }

  return {
    payload: input as MimicPayload,
  };
}

export async function postAnalyze(request: AnalyzeRequest): Promise<AnalyzeResponse> {
  return requestJson<AnalyzeResponse>("/analyze", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export async function postMapping(request: MappingRequest): Promise<MappingResponse> {
  return requestJson<MappingResponse>("/mapping", {
    method: "POST",
    body: JSON.stringify(request),
  });
}
