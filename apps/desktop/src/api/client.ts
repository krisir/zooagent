import type {
  ActionDef,
  AggregateResult,
  AppOverview,
  ApplicationSummary,
  ChatEvent,
  ConversationPayload,
  FieldDef,
  ModelDef,
  ModelProfile,
  QueryResult,
  SettingsPayload,
  SortSpec,
  UISchema,
  WorkspaceInfo,
  WorkspaceOverview,
} from "./types";

interface RuntimeConfig {
  baseUrl?: string;
  token?: string;
}

declare global {
  interface Window {
    __ZOOAGENT_CONFIG__?: RuntimeConfig;
  }
}

const injected = typeof window !== "undefined" ? window.__ZOOAGENT_CONFIG__ : undefined;
const BASE_URL = injected?.baseUrl ?? "";
const TOKEN = injected?.token ?? "";

export class ApiError extends Error {
  status: number;
  code: string;
  details?: unknown;

  constructor(status: number, message: string, code = "error", details?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    ...((init.headers as Record<string, string>) ?? {}),
  };
  if (init.body) headers["Content-Type"] = "application/json";
  if (TOKEN) headers["Authorization"] = `Bearer ${TOKEN}`;

  const response = await fetch(`${BASE_URL}${path}`, { ...init, headers });
  if (!response.ok) {
    let payload: { error?: { message?: string; code?: string; details?: unknown } } | null = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    throw new ApiError(
      response.status,
      payload?.error?.message ?? `${response.status} ${response.statusText}`,
      payload?.error?.code ?? "error",
      payload?.error?.details,
    );
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function chatPath(applicationId?: string): string {
  return applicationId
    ? `/api/applications/${applicationId}/chat`
    : "/api/chat/primary";
}

export const api = {
  workspace: () => request<WorkspaceInfo>("/api/workspace"),
  workspaceOverview: () => request<WorkspaceOverview>("/api/workspace/overview"),

  listApplications: () =>
    request<{ applications: ApplicationSummary[] }>("/api/applications"),
  createApplication: (body: { name: string; description?: string; icon?: string }) =>
    request<{ application: ApplicationSummary }>("/api/applications", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getApplication: (id: string) =>
    request<AppOverview>(`/api/applications/${id}`),
  updateApplication: (id: string, body: Record<string, unknown>) =>
    request<{ application: ApplicationSummary }>(`/api/applications/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteApplication: (id: string) =>
    request<void>(`/api/applications/${id}`, { method: "DELETE" }),

  listModels: (appId: string) =>
    request<{ models: ModelDef[] }>(`/api/applications/${appId}/models`),

  queryRecords: (
    appId: string,
    modelKey: string,
    body: {
      filters?: { field: string; operator: string; value?: unknown }[];
      sort?: SortSpec[];
      limit?: number;
      offset?: number;
    } = {},
  ) =>
    request<QueryResult>(`/api/applications/${appId}/models/${modelKey}/query`, {
      method: "POST",
      body: JSON.stringify({ expand: true, ...body }),
    }),

  aggregate: (
    appId: string,
    modelKey: string,
    body: {
      filters?: { field: string; operator: string; value?: unknown }[];
      group_by?: string[];
      granularity?: string;
      sort?: SortSpec[];
      aggregations?: { function: string; field?: string; alias?: string }[];
      limit?: number;
    } = {},
  ) =>
    request<AggregateResult>(
      `/api/applications/${appId}/models/${modelKey}/aggregate`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  createRecord: (appId: string, modelKey: string, values: Record<string, unknown>) =>
    request<{ record: { id: string; title: string } }>(
      `/api/applications/${appId}/models/${modelKey}/records`,
      { method: "POST", body: JSON.stringify({ values }) },
    ),

  updateRecord: (
    appId: string,
    modelKey: string,
    recordId: string,
    values: Record<string, unknown>,
    expectedRevision?: number,
  ) =>
    request<{ record: { id: string; revision: number } }>(
      `/api/applications/${appId}/models/${modelKey}/records/${recordId}`,
      {
        method: "PATCH",
        body: JSON.stringify({ values, expected_revision: expectedRevision }),
      },
    ),

  deleteRecord: (appId: string, modelKey: string, recordId: string) =>
    request<void>(
      `/api/applications/${appId}/models/${modelKey}/records/${recordId}`,
      { method: "DELETE" },
    ),

  listActions: (appId: string) =>
    request<{ actions: ActionDef[] }>(`/api/applications/${appId}/actions`),

  runAction: (appId: string, actionKey: string, input: Record<string, unknown>) =>
    request<{
      output: unknown;
      effects: { type: string; model: string; record_id: string; title?: string }[];
    }>(`/api/applications/${appId}/actions/${actionKey}/run`, {
      method: "POST",
      body: JSON.stringify({ input }),
    }),

  getUi: (appId: string) => request<{ ui: UISchema }>(`/api/applications/${appId}/ui`),

  getSettings: () => request<SettingsPayload>("/api/settings"),
  saveProfile: (profile: Record<string, unknown>) =>
    request<{ profile: ModelProfile }>("/api/settings/profiles", {
      method: "POST",
      body: JSON.stringify(profile),
    }),
  activateProfile: (profileId: string) =>
    request<{ active_model_profile_id: string }>(
      `/api/settings/profiles/${profileId}/activate`,
      { method: "POST" },
    ),
  deleteProfile: (profileId: string) =>
    request<void>(`/api/settings/profiles/${profileId}`, { method: "DELETE" }),
  testProfile: (profileId?: string) =>
    request<{ ok: boolean; message: string }>("/api/settings/test", {
      method: "POST",
      body: JSON.stringify({ profile_id: profileId }),
    }),

  conversation: (applicationId?: string) =>
    request<ConversationPayload>(chatPath(applicationId)),
  resetConversation: (conversationId: string) =>
    request<ConversationPayload>(`/api/conversations/${conversationId}/reset`, {
      method: "POST",
    }),

  async streamChat(
    applicationId: string | undefined,
    message: string,
    conversationId: string | null,
    onEvent: (event: ChatEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (TOKEN) headers["Authorization"] = `Bearer ${TOKEN}`;
    const response = await fetch(`${BASE_URL}${chatPath(applicationId)}`, {
      method: "POST",
      headers,
      body: JSON.stringify({ message, conversation_id: conversationId }),
      signal,
    });
    if (!response.ok || !response.body) {
      let detail = `${response.status} ${response.statusText}`;
      try {
        const payload = await response.json();
        detail = payload?.error?.message ?? detail;
      } catch {
        /* ignore */
      }
      throw new ApiError(response.status, detail);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (line) onEvent(JSON.parse(line) as ChatEvent);
        newline = buffer.indexOf("\n");
      }
    }
    const tail = buffer.trim();
    if (tail) onEvent(JSON.parse(tail) as ChatEvent);
  },
};

export function optionList(field: FieldDef): { value: string; label: string }[] {
  const raw = field.options ?? [];
  return raw.map((option) =>
    typeof option === "string"
      ? { value: option, label: option }
      : { value: option.value, label: option.label ?? option.value },
  );
}

export function firstTextField(model: ModelDef): FieldDef | undefined {
  return model.fields.find((field) => field.type === "text" && !field.hidden);
}

export function firstDateField(model: ModelDef): FieldDef | undefined {
  return model.fields.find((field) => field.type === "date" || field.type === "datetime");
}

export function aggregateAlias(aggregate: {
  function: string;
  field?: string | null;
  alias?: string | null;
}): string {
  if (aggregate.alias) return aggregate.alias;
  return aggregate.field ? `${aggregate.function}_${aggregate.field}` : aggregate.function;
}

export { BASE_URL, TOKEN };
