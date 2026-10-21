import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../api/client";
import type { ChatEvent, ChatMessageRow } from "../../api/types";

export interface LiveStep {
  id: number;
  name: string;
  ok?: boolean;
}

export interface UseChatOptions {
  /** undefined = primary assistant, otherwise the application's own assistant. */
  applicationId?: string;
  pendingPrompt?: string | null;
  onPromptConsumed?: () => void;
  onChanged?: () => void;
}

export interface UseChatResult {
  messages: ChatMessageRow[];
  steps: LiveStep[];
  sending: boolean;
  loading: boolean;
  /** 用户点了停止，这一轮被中止（不是出错）。 */
  stopped: boolean;
  error: string | null;
  conversationId: string | null;
  send: (text: string) => Promise<void>;
  stop: () => void;
  reset: () => Promise<void>;
}

/**
 * One NDJSON streaming conversation with either the primary assistant or an
 * application assistant. Shared by the home hero chat and the app-side chat,
 * so both stay in sync on tool-call display and refresh signalling.
 */
export function useChat({
  applicationId,
  pendingPrompt,
  onPromptConsumed,
  onChanged,
}: UseChatOptions): UseChatResult {
  const [messages, setMessages] = useState<ChatMessageRow[]>([]);
  const [steps, setSteps] = useState<LiveStep[]>([]);
  const [sending, setSending] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);

  const handledPrompt = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scopeKey = applicationId ?? "primary";

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setStopped(false);
    api
      .conversation(applicationId)
      .then((payload) => {
        if (cancelled) return;
        setConversationId(payload.conversation.id);
        setMessages(payload.messages);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [scopeKey, applicationId]);

  const send = useCallback(
    async (text: string) => {
      const content = text.trim();
      if (!content) return;
      setSending(true);
      setStopped(false);
      setError(null);
      setSteps([]);
      const controller = new AbortController();
      abortRef.current = controller;
      const optimistic: ChatMessageRow = {
        id: `local-${Date.now()}`,
        conversation_id: conversationId ?? "",
        role: "user",
        content,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, optimistic]);

      let stepId = 0;
      try {
        await api.streamChat(applicationId, content, conversationId, (event: ChatEvent) => {
          if (event.type === "start") {
            // 一开始就拿到会话 id，中断后继续发言不会另开一个会话
            if (event.conversation_id) setConversationId(event.conversation_id);
          } else if (event.type === "tool_call") {
            stepId += 1;
            const id = stepId;
            setSteps((prev) => [...prev, { id, name: event.name ?? "tool" }]);
          } else if (event.type === "tool_result") {
            setSteps((prev) =>
              prev.map((step, index) =>
                index === prev.length - 1 && step.ok === undefined
                  ? { ...step, ok: event.ok }
                  : step,
              ),
            );
          } else if (event.type === "assistant_message") {
            setMessages((prev) => [
              ...prev,
              {
                id: `assistant-${Date.now()}`,
                conversation_id: conversationId ?? "",
                role: "assistant",
                content: event.content ?? "",
                metadata: {
                  tools: (event.tools ?? []).map((tool) => ({ name: tool.name, ok: tool.ok })),
                  refresh: event.refresh,
                },
                created_at: new Date().toISOString(),
              },
            ]);
          } else if (event.type === "error") {
            setError(event.message ?? "助手执行出错");
          } else if (event.type === "agent_message") {
            // 另一个助手的答复回来了：两边对话都留一张可折叠的卡片
            const reply = event.reply ?? null;
            setMessages((prev) => [
              ...prev,
              {
                id: event.message_id ?? `agent-${Date.now()}`,
                conversation_id: conversationId ?? "",
                role: "assistant",
                content: reply ?? (event.ok === false ? "没能联系上对方助手" : ""),
                metadata: {
                  kind: "agent_message",
                  direction: event.direction ?? "out",
                  peer: event.peer,
                  request: event.request,
                  reply,
                },
                created_at: new Date().toISOString(),
              },
            ]);
          } else if (event.type === "done") {
            if (event.conversation_id) setConversationId(event.conversation_id);
            if (event.refresh) onChanged?.();
          }
        }, controller.signal);
      } catch (err) {
        if (err instanceof Error && err.name === "AbortError") {
          setStopped(true);
        } else if (err instanceof ApiError) setError(err.message);
        else if (err instanceof Error) setError(err.message);
        else setError("请求失败");
      } finally {
        if (abortRef.current === controller) abortRef.current = null;
        setSending(false);
      }
    },
    [applicationId, conversationId, onChanged],
  );

  useEffect(() => {
    if (pendingPrompt && handledPrompt.current !== pendingPrompt) {
      handledPrompt.current = pendingPrompt;
      onPromptConsumed?.();
      void send(pendingPrompt);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingPrompt]);

  /** 中止当前这一轮：断开流，服务端随之取消 Agent。 */
  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const reset = useCallback(async () => {
    if (!conversationId) return;
    setLoading(true);
    try {
      const payload = await api.resetConversation(conversationId);
      setConversationId(payload.conversation.id);
      setMessages([]);
      setSteps([]);
      setError(null);
      setStopped(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [conversationId]);

  return {
    messages,
    steps,
    sending,
    loading,
    stopped,
    error,
    conversationId,
    send,
    stop,
    reset,
  };
}
