import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button, Tooltip } from "antd";
import { TableOutlined } from "@ant-design/icons";

interface SplitViewProps {
  /** 助手常驻；数据页是折叠起来的那一半。 */
  dataVisible: boolean;
  dock: ReactNode;
  children: ReactNode;
}

/**
 * 助手 + 数据页的两栏壳体。应用助手和主助手共用这一层，
 * 显隐数据页时的时序、几何和动效就不会各自漂移。
 */
export function SplitView({ dataVisible, dock, children }: SplitViewProps) {
  // 面板隐藏后多留一拍用于淡出；显示时先让它待在文档流外一帧，
  // 助手列的 flex-grow 过渡才有"之前"的状态可读。
  const [mounted, setMounted] = useState(dataVisible);
  const [leaving, setLeaving] = useState(false);
  const [entering, setEntering] = useState(false);
  const mountedRef = useRef(dataVisible);

  useEffect(() => {
    if (dataVisible) {
      setMounted(true);
      setLeaving(false);
      if (!mountedRef.current) {
        mountedRef.current = true;
        setEntering(true);
        // matches dock-show / panel-in (440ms) plus the card's 40ms delay
        const timer = window.setTimeout(() => setEntering(false), 520);
        return () => window.clearTimeout(timer);
      }
      return;
    }
    mountedRef.current = false;
    setEntering(false);
    setLeaving(true);
    // matches panel-out (360ms) plus a little slack before unmounting
    const timer = window.setTimeout(() => {
      setMounted(false);
      setLeaving(false);
    }, 400);
    return () => window.clearTimeout(timer);
  }, [dataVisible]);

  return (
    <div
      className={`appview ${dataVisible && !entering ? "has-data" : "data-hidden"}${
        entering ? " entering" : ""
      }`}
    >
      <aside className="assistant-dock">{dock}</aside>
      {mounted ? (
        <div
          className={`appview-main${leaving ? " leaving" : ""}${entering ? " entering" : ""}`}
          aria-hidden={leaving || undefined}
        >
          {children}
        </div>
      ) : null}
    </div>
  );
}

/** 显隐数据页的开关：两个页面都把它挂在助手头部，位置和行为一致。 */
export function DataSwitch({
  visible,
  onToggle,
}: {
  visible: boolean;
  onToggle: () => void;
}) {
  const label = visible ? "隐藏数据页" : "显示数据页";
  return (
    <Tooltip title={label}>
      <Button
        size="small"
        type={visible ? "text" : "primary"}
        icon={<TableOutlined />}
        aria-label={label}
        onClick={onToggle}
      />
    </Tooltip>
  );
}
