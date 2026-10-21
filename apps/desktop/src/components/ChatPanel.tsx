import { useEffect, useRef, useState, type ReactNode } from "react";
import { Button, Empty, Input, Spin } from "antd";
import type { TextAreaRef } from "antd/es/input/TextArea";
import { ReloadOutlined, SendOutlined, StopFilled } from "@ant-design/icons";
import { useChat } from "../features/chat/useChat";
import { Thread } from "./Thread";

export interface ChatSuggestion {
  label: string;
  prompt: string;
}

export interface ChatPanelProps {
  /** undefined = primary assistant, otherwise the app's own assistant. */
  applicationId?: string;
  title: string;
  greeting?: string;
  hint?: string;
  /** Hide the built-in header when the host (e.g. a drawer) draws its own. */
  showHeader?: boolean;
  /** Click-to-send starters, shown only while the conversation is empty. */
  suggestions?: ChatSuggestion[];
  /** Controls drawn at the right of the header (e.g. the layout switch). */
  headerExtra?: ReactNode;
  /** A prompt handed over from elsewhere (e.g. the create-assistant drawer). */
  pendingPrompt?: string | null;
  onPromptConsumed?: () => void;
  onChanged?: () => void;
  /** 跳到另一个助手的对话（助手之间消息卡片上的「打开对话」）。 */
  onOpenAssistant?: (peer: { name: string; application_id?: string | null }) => void;
}

export function ChatPanel({
  applicationId,
  title,
  greeting,
  hint,
  showHeader = true,
  suggestions,
  headerExtra,
  pendingPrompt,
  onPromptConsumed,
  onChanged,
  onOpenAssistant,
}: ChatPanelProps) {
  const [input, setInput] = useState("");
  const chat = useChat({ applicationId, pendingPrompt, onPromptConsumed, onChanged });
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<TextAreaRef | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [chat.messages, chat.steps, chat.sending]);

  // The composer owns the keyboard: focus on mount, and keep it after sending
  // (also after the reply lands, in case the assistant was clicked meanwhile).
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!chat.sending) inputRef.current?.focus();
  }, [chat.sending]);

  const submit = () => {
    const text = input.trim();
    if (!text || chat.sending) return;
    setInput("");
    void chat.send(text);
    inputRef.current?.focus();
  };

  return (
    <div className="chat-panel">
      {showHeader ? (
        <div className="chat-header">
          <div className="chat-identity">
            <span className="chat-mark">z</span>
            <div>
              <div className="chat-title">{title}</div>
              {hint ? <div className="chat-subtitle">{hint}</div> : null}
            </div>
          </div>
          <div className="chat-header-actions">
            {headerExtra}
            <Button
              size="small"
              type="text"
              icon={<ReloadOutlined />}
              onClick={() => void chat.reset()}
              title="开始新对话"
            />
          </div>
        </div>
      ) : null}

      <div className="chat-body">
        {chat.loading ? <Spin /> : null}
        {!chat.messages.length && greeting && !chat.loading ? (
          <div className="chat-greeting">{greeting}</div>
        ) : null}
        <Thread
          messages={chat.messages}
          steps={chat.steps}
          sending={chat.sending}
          stopped={chat.stopped}
          onOpenAssistant={onOpenAssistant}
        />
        {chat.error ? <div className="tool-chip fail">{chat.error}</div> : null}
        {!chat.loading && !chat.messages.length && !greeting ? (
          <Empty description="开始新的对话" />
        ) : null}
        {suggestions?.length && !chat.messages.length && !chat.sending ? (
          <div className="chat-suggest">
            {suggestions.map((item) => (
              <button
                key={item.label}
                type="button"
                onClick={() => void chat.send(item.prompt)}
              >
                {item.label}
              </button>
            ))}
          </div>
        ) : null}
        <div ref={bottomRef} />
      </div>

      <div className="chat-input">
        <div className="chat-composer">
          <Input.TextArea
            ref={inputRef}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="描述你的需求…"
            autoSize={{ minRows: 2, maxRows: 4 }}
            onPressEnter={(event) => {
              if (!event.shiftKey) {
                event.preventDefault();
                submit();
              }
            }}
          />
          {/* 同一颗按钮：空闲时发送，处理中变成停止（不会同时是加载动画） */}
          <Button
            className={`chat-send${chat.sending ? " stopping" : ""}`}
            type={chat.sending ? "default" : "primary"}
            icon={chat.sending ? <StopFilled /> : <SendOutlined />}
            aria-label={chat.sending ? "停止" : "发送"}
            onClick={chat.sending ? chat.stop : submit}
            disabled={!chat.sending && !input.trim()}
          />
        </div>
      </div>
    </div>
  );
}
