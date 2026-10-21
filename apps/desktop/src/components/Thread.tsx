import { useState } from "react";
import { ArrowRightOutlined, DownOutlined, SwapOutlined } from "@ant-design/icons";
import type { ChatMessageRow } from "../api/types";
import type { LiveStep } from "../features/chat/useChat";
import { toolLabel, visibleTools } from "../features/chat/toolLabels";
import { Markdown } from "./Markdown";

interface ThreadProps {
  messages: ChatMessageRow[];
  steps: LiveStep[];
  sending: boolean;
  /** 这一轮被用户中止：不是错误，给一行安静的回执。 */
  stopped?: boolean;
  /** 点消息卡片直接跳到对方的对话；没有跳转能力时才就地展开。 */
  onOpenAssistant?: (peer: { name: string; application_id?: string | null }) => void;
}

/**
 * One exchange between two assistants, collapsed to a single line.
 *
 * The transcript is written for the user, who does not need to read every
 * word two assistants exchanged — but must be able to check it. The card is
 * stored on the message itself (`metadata.kind === "agent_message"`), so it
 * survives a reload and reads the same in both conversations:
 * 「收到了「个人记账」的消息」 / 「回复了「主助手」」.
 */
function AgentMessageCard({
  message,
  onOpenAssistant,
}: {
  message: ChatMessageRow;
  onOpenAssistant?: ThreadProps["onOpenAssistant"];
}) {
  const meta = message.metadata ?? {};
  const peer = meta.peer?.name ?? "助手";
  const outbound = meta.direction !== "in";
  const [open, setOpen] = useState(false);
  const request = meta.request ?? (outbound ? "" : message.content ?? "");
  const reply = meta.reply ?? (outbound ? message.content ?? "" : "");
  const title = outbound ? `回复了「${peer}」` : `收到了「${peer}」的消息`;
  const canJump = Boolean(onOpenAssistant);

  return (
    <div className={`agent-msg${outbound ? " out" : " in"}`}>
      {/* 有对方对话可跳转时，点一下直接过去看原文；没有才就地展开 */}
      <button
        type="button"
        className="agent-msg-head"
        aria-expanded={canJump ? undefined : open}
        aria-label={canJump ? `${title}，打开与「${peer}」的对话` : title}
        onClick={() => {
          if (onOpenAssistant) {
            onOpenAssistant({
              name: peer,
              application_id: meta.peer?.application_id ?? null,
            });
          } else {
            setOpen((value) => !value);
          }
        }}
      >
        <SwapOutlined />
        <span className="agent-msg-title">{title}</span>
        {canJump ? (
          <span className="agent-msg-go">
            打开对话
            <ArrowRightOutlined />
          </span>
        ) : (
          <DownOutlined className="agent-msg-chev" />
        )}
      </button>
      {!canJump && open ? (
        <div className="agent-msg-body">
          {outbound ? (
            <>
              <div className="agent-msg-label">我发出的</div>
              <div className="agent-msg-text">{request}</div>
              <div className="agent-msg-label">对方回复</div>
              <div className="agent-msg-text">{reply || "（没有回复）"}</div>
            </>
          ) : (
            <>
              <div className="agent-msg-label">消息内容</div>
              <div className="agent-msg-text">{request}</div>
            </>
          )}
        </div>
      ) : null}
    </div>
  );
}

export function Thread({ messages, steps, sending, stopped, onOpenAssistant }: ThreadProps) {
  const visible = messages.filter((message) => message.role === "user" || message.content);
  const liveSteps = visibleTools(steps);
  const latest = liveSteps.length ? liveSteps[liveSteps.length - 1] : null;
  return (
    <>
      {visible.map((message) =>
        message.metadata?.kind === "agent_message" ? (
          <AgentMessageCard
            key={message.id}
            message={message}
            onOpenAssistant={onOpenAssistant}
          />
        ) : (
          <div key={message.id} className={`thread-row ${message.role}`}>
            {message.role === "assistant" ? <span className="chat-mark">z</span> : null}
            <div className="bubble">
              {message.content ? <Markdown text={message.content} /> : null}
            </div>
          </div>
        ),
      )}
      {sending ? (
        <div className="thread-row assistant">
          <span className="chat-mark">z</span>
          <div className="bubble">
            {latest ? (
              // one chip at a time: each new tool call replaces the previous one
              <div className="tool-chips">
                <span
                  key={`${latest.name}-${liveSteps.length}`}
                  className={`tool-chip live ${latest.ok === false ? "fail" : latest.ok ? "ok" : "pending"}`}
                >
                  {toolLabel(latest.name)}
                </span>
              </div>
            ) : (
              <span className="bubble-text muted">正在想…</span>
            )}
          </div>
        </div>
      ) : null}
      {stopped && !sending ? (
        <div className="thread-row assistant">
          <span className="chat-mark">z</span>
          <div className="bubble">
            <span className="bubble-text muted">已停止</span>
          </div>
        </div>
      ) : null}
    </>
  );
}
