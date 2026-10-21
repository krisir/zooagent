import type { ApplicationSummary } from "../api/types";
import { appEmoji } from "../lib/format";

interface RailProps {
  applications: ApplicationSummary[];
  activeAppId: string | null;
  onOpenPrimary: () => void;
  onOpenApp: (id: string) => void;
  onOpenSettings: () => void;
}

/**
 * Left rail: the primary assistant, then every business assistant in one flat
 * list. There is no workspace switcher — the app is single-tenant by design, so
 * the rail is simply "who can I talk to".
 */
export function Rail({
  applications,
  activeAppId,
  onOpenPrimary,
  onOpenApp,
  onOpenSettings,
}: RailProps) {
  const ordered = [...applications].sort((left, right) =>
    (right.updated_at ?? "").localeCompare(left.updated_at ?? ""),
  );

  return (
    <aside className="rail">
      <div className="brand">
        <span className="logo">z</span>
        <span className="brand-text">zooagent</span>
      </div>

      <button
        type="button"
        className={`nav primary${activeAppId ? "" : " active"}`}
        onClick={onOpenPrimary}
      >
        <i>✳</i>
        <span>主助手</span>
      </button>

      <div className="nav-split" />

      <div className="navlabel">助手{applications.length ? ` · ${applications.length}` : ""}</div>

      <div className="rail-list">
        {ordered.map((application) => (
          <button
            key={application.id}
            type="button"
            className={`nav app${activeAppId === application.id ? " active" : ""}`}
            onClick={() => onOpenApp(application.id)}
            title={application.description ?? application.name}
          >
            <i className="nav-emoji">{appEmoji(application)}</i>
            <span>{application.name}</span>
          </button>
        ))}
        {ordered.length ? null : (
          <div className="rail-empty">还没有助手。跟主助手说说你想管什么。</div>
        )}
      </div>

      <div className="rail-bottom">
        <button
          type="button"
          className="rail-settings"
          onClick={onOpenSettings}
          title="偏好设置"
          aria-label="偏好设置"
        >
          ⚙
        </button>
      </div>
    </aside>
  );
}
