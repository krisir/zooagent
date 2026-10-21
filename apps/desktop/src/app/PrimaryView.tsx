import { useCallback, useEffect, useState } from "react";
import { Button, Empty, Spin, Table, message } from "antd";
import type { TableColumnsType } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { api } from "../api/client";
import type { WorkspaceAssistant, WorkspaceOverview } from "../api/types";
import { ChatPanel, type ChatSuggestion } from "../components/ChatPanel";
import { DataSwitch, SplitView } from "../components/SplitView";
import { appEmoji } from "../lib/format";

interface PrimaryViewProps {
  pendingPrompt: string | null;
  onPromptConsumed: () => void;
  onChanged: () => void;
  refreshToken: number;
  /** 助手常驻；数据页是折叠起来的那一半。 */
  dataVisible: boolean;
  onToggleData: () => void;
  onOpenApp: (applicationId: string) => void;
  onAsk: (prompt: string) => void;
}

// One entry point: 主助手先问清楚再建，所以快捷提示词只管"开个头"，
// 不再预设记账 / 客户 / 物品这类具体场景。
const SUGGESTIONS: ChatSuggestion[] = [
  { label: "＋ 创建新助手", prompt: "我想创建一个新助手" },
];

const COLUMNS: TableColumnsType<WorkspaceAssistant> = [
  {
    title: "助手",
    dataIndex: "name",
    render: (_, row) => (
      <div className="roster-identity">
        <span className="roster-emoji">{appEmoji(row)}</span>
        <div className="roster-text">
          <div className="roster-name">{row.name}</div>
          {row.description ? <div className="roster-desc">{row.description}</div> : null}
        </div>
      </div>
    ),
  },
  { title: "模型", width: 72, align: "right", render: (_, row) => row.counts.models },
  { title: "操作", width: 72, align: "right", render: (_, row) => row.counts.actions },
  { title: "页面", width: 72, align: "right", render: (_, row) => row.counts.pages },
  {
    title: "最近更新",
    dataIndex: "updated_at",
    width: 132,
    render: (value: string) => (
      <span className="roster-time">{dayjs(value).format("MM-DD HH:mm")}</span>
    ),
  },
];

/**
 * 主助手：和每个应用助手同一套两栏壳体，只是数据页换成工作台目录。
 * 它创建和升级助手，所以自己的数据页只有助手的结构信息，没有业务数据。
 */
export function PrimaryView({
  pendingPrompt,
  onPromptConsumed,
  onChanged,
  refreshToken,
  dataVisible,
  onToggleData,
  onOpenApp,
  onAsk,
}: PrimaryViewProps) {
  const [overview, setOverview] = useState<WorkspaceOverview | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setOverview(await api.workspaceOverview());
    } catch (err) {
      message.error(err instanceof Error ? err.message : "读取工作台目录失败");
    } finally {
      setLoading(false);
    }
  }, []);

  // 主助手刚建完助手时 onChanged 会推进 refreshToken，目录跟着更新。
  useEffect(() => {
    void load();
  }, [load, refreshToken]);

  const assistants = overview?.assistants ?? [];

  return (
    <SplitView
      dataVisible={dataVisible}
      dock={
        <ChatPanel
          title="主助手"
          headerExtra={<DataSwitch visible={dataVisible} onToggle={onToggleData} />}
          greeting="我是主助手。告诉我你想管什么，我会把它做成一个助手：数据模型、业务操作、界面都由我来搭。"
          suggestions={SUGGESTIONS}
          pendingPrompt={pendingPrompt}
          onPromptConsumed={onPromptConsumed}
          onChanged={onChanged}
          // 助手之间消息卡片：直接跳到对方助手的对话看原文
          onOpenAssistant={(peer) => {
            if (peer.application_id) onOpenApp(peer.application_id);
          }}
        />
      }
    >
      <div className="appview-head">
        <div className="appview-title">
          <span className="appemoji" style={{ fontSize: 26 }}>
            🗂
          </span>
          <div>
            <h1>工作台</h1>
          </div>
        </div>
        <div className="appview-actions">
          <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading} />
        </div>
      </div>

      {loading && !overview ? (
        <div className="centered">
          <Spin />
        </div>
      ) : assistants.length === 0 ? (
        <Empty description="还没有助手" style={{ marginTop: 56 }}>
          <Button type="primary" onClick={() => onAsk("我想创建一个新助手")}>
            让主助手创建一个
          </Button>
        </Empty>
      ) : (
        <Table<WorkspaceAssistant>
          className="roster-table"
          rowKey="id"
          size="small"
          columns={COLUMNS}
          dataSource={assistants}
          pagination={false}
          loading={loading}
          onRow={(row) => ({ onClick: () => onOpenApp(row.id) })}
          style={{ marginTop: 8 }}
        />
      )}
    </SplitView>
  );
}
