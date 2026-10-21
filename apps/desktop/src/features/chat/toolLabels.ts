/** Human labels for the agent's tool calls, so the reasoning trace reads in Chinese. */
export const TOOL_LABELS: Record<string, string> = {
  list_applications: "查看已有应用",
  create_application: "创建应用",
  get_application_overview: "读取应用结构",
  define_model: "定义数据模型",
  define_action: "定义业务操作",
  set_pages: "生成界面",
  set_app_agent: "配置专属助手",
  seed_records: "写入初始数据",
  list_models: "读取模型",
  describe_model: "查看模型字段",
  list_actions: "查看业务操作",
  query_records: "查询数据",
  aggregate: "统计汇总",
  create_record: "新增记录",
  update_record: "修改记录",
  delete_record: "删除记录",
  run_action: "执行业务操作",
  diagnose_action: "主助手排查失败原因",
  send_message_to_primary: "转给主助手",
  send_message_to_application: "联系应用助手",
  await_reply: "等待对方助手回复",
};

export function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name;
}

/**
 * Routine data operations the user does not need to watch. They are still sent
 * by the backend (and still recorded on the message), but the transcript hides
 * them: chatting about data should read like a conversation, not a debug log.
 * Failures are always shown, so a rejected write never goes unnoticed.
 */
export const QUIET_TOOLS = new Set([
  "list_models",
  "describe_model",
  "list_actions",
  "query_records",
  "aggregate",
  "create_record",
  "update_record",
  "delete_record",
  "run_action",
]);

export function visibleTools<T extends { name: string; ok?: boolean }>(tools: T[]): T[] {
  return tools.filter((tool) => !QUIET_TOOLS.has(tool.name) || tool.ok === false);
}
