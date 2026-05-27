import { normalizeAnalyzeInput, type AnalyzeRequest, type MappingRequest } from "@/lib/api";

export function parseResourceTypes(input: string): string[] | undefined {
  const resourceTypes = input
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);

  return resourceTypes.length > 0 ? resourceTypes : undefined;
}

export function parsePayloadText(payloadText: string): unknown {
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

export function parseSubjectIdOverride(input: string): number | undefined {
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

export function applySubjectIdOverride(request: AnalyzeRequest, subjectIdInput: string): AnalyzeRequest {
  const subjectId = parseSubjectIdOverride(subjectIdInput);

  if (subjectId === undefined) {
    return request;
  }

  const patient = request.payload.patient;

  if (!patient || typeof patient !== "object" || Array.isArray(patient)) {
    throw new Error("Subject ID override requires payload.patient to be an object in the JSON input.");
  }

  const currentSubjectId = patient.subject_id;
  const shouldResetHospitalizationContext = currentSubjectId !== undefined && currentSubjectId !== subjectId;

  const labEvent = request.payload.lab_event;
  const nextLabEvent =
    shouldResetHospitalizationContext && labEvent && typeof labEvent === "object" && !Array.isArray(labEvent)
      ? Object.fromEntries(Object.entries(labEvent).filter(([key]) => key !== "hadm_id"))
      : labEvent;

  return {
    ...request,
    payload: {
      ...request.payload,
      admission: shouldResetHospitalizationContext ? null : request.payload.admission,
      admissions: shouldResetHospitalizationContext ? null : request.payload.admissions,
      lab_event: nextLabEvent,
      patient: {
        ...patient,
        subject_id: subjectId,
      },
    },
  };
}

export function buildAnalyzeRequest(
  payloadText: string,
  resourceTypesText: string,
  subjectIdOverride: string,
): AnalyzeRequest {
  const parsed = parsePayloadText(payloadText);
  const request = normalizeAnalyzeInput(parsed);
  const resourceTypes = parseResourceTypes(resourceTypesText) ?? request.resource_types;

  return applySubjectIdOverride(
    {
      ...request,
      resource_types: resourceTypes,
    },
    subjectIdOverride,
  );
}

export function buildMappingRequest(
  analyzeRequest: AnalyzeRequest,
  mappingItemId: string,
): MappingRequest {
  const request: MappingRequest = {
    payload: analyzeRequest.payload,
  };

  if (mappingItemId.trim()) {
    request.itemid = mappingItemId.trim();
  }

  return request;
}
