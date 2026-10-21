export type FieldType =
  | "text"
  | "integer"
  | "decimal"
  | "boolean"
  | "date"
  | "datetime"
  | "enum"
  | "relation"
  | "json";

export interface FieldOption {
  value: string;
  label?: string;
}

export interface FieldDef {
  key: string;
  name: string;
  type: FieldType;
  description?: string | null;
  required?: boolean;
  unique?: boolean;
  hidden?: boolean;
  default?: unknown;
  options?: FieldOption[] | string[] | null;
  scale?: number | null;
  target_model?: string | null;
  multiple?: boolean;
}

export interface ModelDef {
  key: string;
  name: string;
  description?: string | null;
  title_field: string;
  fields: FieldDef[];
}

export interface ApplicationSummary {
  id: string;
  name: string;
  description?: string | null;
  icon?: string | null;
  color?: string | null;
  status: string;
  active_version: number;
  created_at: string;
  updated_at: string;
}

export interface AgentInfo {
  id: string;
  application_id: string | null;
  name: string;
  role: string;
  instructions?: string | null;
  greeting?: string | null;
  model_profile_id?: string | null;
}

export interface RecordItem {
  id: string;
  model: string;
  model_id: string;
  data: Record<string, unknown>;
  revision: number;
  created_at: string;
  updated_at: string;
  deleted_at?: string | null;
  title: string;
  labels?: Record<string, string | string[]>;
}

export interface QueryResult {
  items: RecordItem[];
  total: number;
  limit: number;
  offset: number;
  model: string;
}

export interface Filter {
  field: string;
  operator: string;
  value?: unknown;
}

export interface SortSpec {
  field: string;
  direction: "asc" | "desc";
}

export interface AggregateItem {
  [key: string]: unknown;
}

export interface AggregateResult {
  items: AggregateItem[];
  group_by: string[];
  aggregations: string[];
  model: string;
}

export interface UIActionRef {
  label: string;
  kind: "create" | "edit" | "delete" | "run_action" | "open_page" | "chat";
  action?: string | null;
  page?: string | null;
  prompt?: string | null;
}

export interface UIComponent {
  id?: string;
  type: string;
  title?: string | null;
  description?: string | null;
  model?: string | null;
  fields?: string[] | null;
  columns?: { field: string; width?: number; label?: string }[] | null;
  actions?: UIActionRef[];
  props?: Record<string, unknown>;
}

export interface UIPage {
  id?: string;
  key: string;
  title: string;
  icon?: string | null;
  description?: string | null;
  layout: "vertical" | "grid" | "two_column";
  components: UIComponent[];
}

export interface UISchema {
  pages: UIPage[];
  home_page?: string | null;
}

export interface AppOverview {
  application: ApplicationSummary;
  agent: AgentInfo | null;
  models: ModelDef[];
  actions: ActionDef[];
  ui: UISchema;
}

export interface ActionDef {
  id?: string;
  key: string;
  name: string;
  description?: string | null;
  version: number;
  read_only?: boolean;
  inputs: FieldDef[];
  steps?: unknown[];
}

/** 助手之间的一次往来，以可折叠卡片显示（metadata.kind === "agent_message"）。 */
export interface AgentMessageMeta {
  kind?: string;
  direction?: "in" | "out";
  peer?: { id?: string; name: string; application_id?: string | null };
  /** 发出去的内容（direction=out） */
  request?: string;
  /** 对方答复（direction=out） */
  reply?: string | null;
}

export interface ChatMessageRow {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | string;
  content?: string | null;
  metadata?: (AgentMessageMeta & {
    tools?: { name: string; ok: boolean; arguments?: Record<string, unknown> }[];
    refresh?: boolean;
    errored?: boolean;
  }) | null;
  created_at: string;
}

export interface ConversationInfo {
  id: string;
  agent_id: string;
  application_id?: string | null;
  title?: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationPayload {
  conversation: ConversationInfo;
  messages: ChatMessageRow[];
}

export interface ChatEvent {
  type: string;
  content?: string;
  name?: string;
  ok?: boolean;
  arguments?: Record<string, unknown>;
  result?: unknown;
  error?: { message?: string } | null;
  message?: string;
  refresh?: boolean;
  conversation_id?: string;
  message_id?: string | null;
  /** 助手之间消息：方向、对方、内容、答复 */
  direction?: "in" | "out";
  peer?: { id?: string; name: string; application_id?: string | null };
  request?: string;
  reply?: string | null;
  tools?: { name: string; arguments?: Record<string, unknown>; ok: boolean }[];
}

export interface ModelProfile {
  id: string;
  name: string;
  /** agentscope / openai / ollama / mock / offline — set by presets, not edited. */
  provider: string;
  model_family?: string | null;
  base_url: string;
  model_name: string;
  api_key_set?: boolean;
  /** last 4 characters only; the key itself never leaves the backend */
  api_key_last4?: string | null;
  temperature?: number;
  max_tokens?: number;
}

export interface ProviderPreset {
  key: string;
  label: string;
  provider: string;
  model_family?: string | null;
  base_url: string;
  models: string[];
}

export interface SettingsPayload {
  active_model_profile_id: string;
  language: string;
  theme: string;
  profiles: ModelProfile[];
  providers: ProviderPreset[];
}

export interface WorkspaceInfo {
  version: string;
  workspace: string;
  active_model_profile_id: string;
  active_model_name?: string;
  offline_mode: boolean;
  agent_runtime?: string;
  platform: string;
}

/** 一个助手的数据规模；主助手数据页用它画目录，不含业务数据。 */
export interface AssistantCounts {
  models: number;
  actions: number;
  pages: number;
}

export interface WorkspaceAssistant extends ApplicationSummary {
  counts: AssistantCounts;
}

export interface WorkspaceOverview {
  assistants: WorkspaceAssistant[];
  totals: { assistants: number } & AssistantCounts;
}
