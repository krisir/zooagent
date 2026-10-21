import { useEffect, useState } from "react";
import { Button, Empty, Space, Spin, Tabs } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import type { AppOverview } from "../api/types";
import { ChatPanel } from "../components/ChatPanel";
import { Renderer } from "../components/Renderer";
import { DataSwitch, SplitView } from "../components/SplitView";
import { appEmoji } from "../lib/format";

interface ApplicationViewProps {
  overview: AppOverview | null;
  loading: boolean;
  refreshToken: number;
  /** The assistant is always there; the data page is the part that folds away. */
  dataVisible: boolean;
  onRefresh: () => void;
  onChanged: () => void;
  onToggleData: () => void;
  onAskPrimary: (prompt: string) => void;
  /** 跳到主助手对话（助手之间消息卡片上的「打开对话」）。 */
  onOpenPrimary: () => void;
}

export function ApplicationView({
  overview,
  loading,
  refreshToken,
  dataVisible,
  onRefresh,
  onChanged,
  onToggleData,
  onAskPrimary,
  onOpenPrimary,
}: ApplicationViewProps) {
  const pages = overview?.ui.pages ?? [];
  const [activePage, setActivePage] = useState<string | undefined>(undefined);

  useEffect(() => {
    const preferred = overview?.ui.home_page ?? pages[0]?.key;
    if (!pages.some((page) => page.key === activePage)) setActivePage(preferred);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overview]);

  if (loading && !overview) {
    return (
      <div className="centered">
        <Spin />
      </div>
    );
  }
  if (!overview) {
    return (
      <div className="centered">
        <Empty description="应用不存在" />
      </div>
    );
  }

  const page = pages.find((item) => item.key === activePage) ?? pages[0];
  const { application } = overview;
  const agentName = overview.agent?.name ?? `${application.name} 助手`;

  // One icon decides whether the data page is on screen. The assistant lives
  // here permanently, so the switch travels with its header.
  const dataSwitch = <DataSwitch visible={dataVisible} onToggle={onToggleData} />;

  return (
    <SplitView
      dataVisible={dataVisible}
      dock={
        <ChatPanel
          key={application.id}
          applicationId={application.id}
          title={agentName}
          headerExtra={dataSwitch}
          greeting={`我是「${application.name}」的专属助手。`}
          onChanged={onChanged}
          onOpenAssistant={(peer) => {
            if (!peer.application_id) onOpenPrimary();
          }}
        />
      }
    >
      <div className="appview-head">
        <div className="appview-title">
          <span className="appemoji" style={{ fontSize: 26 }}>
            {appEmoji(application)}
          </span>
          <div>
            <h1>{application.name}</h1>
          </div>
        </div>
        <div className="appview-actions">
          <Button icon={<ReloadOutlined />} onClick={onRefresh} loading={loading} />
        </div>
      </div>

      {pages.length === 0 ? (
        <Empty description="这个应用还没有界面" style={{ marginTop: 56 }}>
          <Space>
            <Button
              type="primary"
              onClick={() =>
                onAskPrimary(
                  `请为「${application.name}」生成界面：包含概览页（常用统计卡片）和数据表格页。`,
                )
              }
            >
              让主助手生成界面
            </Button>
          </Space>
        </Empty>
      ) : (
        <Tabs
          activeKey={page?.key}
          onChange={setActivePage}
          items={pages.map((item) => ({
            key: item.key,
            label: item.title,
            children: (
              <Renderer
                appId={application.id}
                appName={application.name}
                models={overview.models}
                actions={overview.actions}
                components={item.components}
                layout={item.layout}
                refreshToken={refreshToken}
                onChanged={onChanged}
              />
            ),
          }))}
        />
      )}
    </SplitView>
  );
}
