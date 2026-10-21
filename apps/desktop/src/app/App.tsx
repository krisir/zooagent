import { useCallback, useEffect, useRef, useState } from "react";
import { Layout, message } from "antd";
import { api } from "../api/client";
import type { AppOverview, ApplicationSummary } from "../api/types";
import { SettingsModal } from "../components/SettingsModal";
import { ApplicationView } from "./ApplicationView";
import { PrimaryView } from "./PrimaryView";
import { Rail } from "./Rail";

export default function App() {
  const [applications, setApplications] = useState<ApplicationSummary[]>([]);
  const [currentAppId, setCurrentAppId] = useState<string | null>(null);
  const [overview, setOverview] = useState<AppOverview | null>(null);
  const [overviewLoading, setOverviewLoading] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const [pendingPrompt, setPendingPrompt] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  // the assistant is always open; this only decides whether the data page shows
  const [dataVisible, setDataVisible] = useState(true);
  const appCount = useRef(0);

  const loadApplications = useCallback(async () => {
    try {
      const result = await api.listApplications();
      setApplications(result.applications);
      if (appCount.current && result.applications.length > appCount.current) {
        message.success("助手已经建好啦，打开看看吧。");
      }
      appCount.current = result.applications.length;
    } catch (err) {
      message.error(err instanceof Error ? err.message : "读取应用列表失败");
    }
  }, []);

  useEffect(() => {
    void loadApplications();
  }, [loadApplications]);

  useEffect(() => {
    if (!currentAppId) {
      setOverview(null);
      return;
    }
    let cancelled = false;
    setOverviewLoading(true);
    api
      .getApplication(currentAppId)
      .then((data) => {
        if (!cancelled) setOverview(data);
      })
      .catch((err: Error) => {
        if (!cancelled) message.error(err.message);
      })
      .finally(() => {
        if (!cancelled) setOverviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [currentAppId, refreshToken]);

  const handleChanged = useCallback(() => {
    void loadApplications();
    setRefreshToken((value) => value + 1);
  }, [loadApplications]);

  // Assistants are deleted by asking the primary assistant (the app pages have
  // no delete button), so a refresh can drop the app whose page is open.
  useEffect(() => {
    setCurrentAppId((id) =>
      id && !applications.some((application) => application.id === id) ? null : id,
    );
  }, [applications]);

  const openApplication = useCallback(
    (applicationId: string) => {
      // 消息卡片可能指向一个已经被删掉的助手
      if (!applications.some((application) => application.id === applicationId)) {
        message.warning("这个助手已经不在了");
        return;
      }
      setCurrentAppId(applicationId);
    },
    [applications],
  );

  const openPrimary = useCallback(() => {
    setCurrentAppId(null);
  }, []);

  const askPrimary = useCallback((prompt: string) => {
    setCurrentAppId(null);
    setPendingPrompt(prompt);
  }, []);

  // the primary assistant owns the whole canvas; an application is a shell with
  // three columns (rail · assistant · data) that all share the rail's height.
  return (
    <Layout className="app-shell">
      <Rail
        applications={applications}
        activeAppId={currentAppId}
        onOpenPrimary={openPrimary}
        onOpenApp={openApplication}
        onOpenSettings={() => setSettingsOpen(true)}
      />

      <div className="main">
        <div className="scroller">
          <div className="content app-content">
            {currentAppId ? (
              <ApplicationView
                overview={overview}
                loading={overviewLoading}
                refreshToken={refreshToken}
                dataVisible={dataVisible}
                onRefresh={() => setRefreshToken((value) => value + 1)}
                onChanged={handleChanged}
                onToggleData={() => setDataVisible((value) => !value)}
                onAskPrimary={askPrimary}
                onOpenPrimary={openPrimary}
              />
            ) : (
              <PrimaryView
                pendingPrompt={pendingPrompt}
                onPromptConsumed={() => setPendingPrompt(null)}
                onChanged={handleChanged}
                refreshToken={refreshToken}
                dataVisible={dataVisible}
                onToggleData={() => setDataVisible((value) => !value)}
                onOpenApp={openApplication}
                onAsk={askPrimary}
              />
            )}
          </div>
        </div>
      </div>

      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onChanged={() => undefined}
      />
    </Layout>
  );
}
