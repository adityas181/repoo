import { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { Send, Sparkles, ChevronDown, Plus, MessageSquare, PanelLeftClose, PanelLeft, User, Loader2, Trash2, Check, ImagePlus, X, Clock } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  useGetOrchAgents,
  useGetOrchSessions,
  useGetOrchMessages,
  useDeleteOrchSession,
} from "@/controllers/API/queries/orchestrator";
import type {
  OrchAgentSummary,
  OrchSessionSummary,
  OrchMessageResponse,
} from "@/controllers/API/queries/orchestrator";
import { usePostUploadFileV2 } from "@/controllers/API/queries/file-management/use-post-upload-file";
import { api, performStreamingRequest } from "@/controllers/API/api";
import { getURL } from "@/controllers/API/helpers/constants";
import { BASE_URL_API } from "@/constants/constants";
import { AuthContext } from "@/contexts/authContext";
import { MarkdownField } from "@/modals/IOModal/components/chatView/chatMessage/components/edit-message";
import { ContentBlockDisplay } from "@/components/core/chatComponents/ContentBlockDisplay";
import type { ContentBlock } from "@/types/chat";

/* ------------------ TYPES ------------------ */

interface Agent {
  id: string;
  name: string;
  description: string;
  online: boolean;
  color: string;
  deploy_id: string;
  agent_id: string;
  version_number: number;
  version_label: string;
  environment: "uat" | "prod" | string;
}

interface Message {
  id: string;
  sender: "user" | "agent" | "system";
  agentName?: string;
  content: string;
  timestamp: string;
  category?: string;
  contentBlocks?: ContentBlock[];
  blocksState?: string;
  files?: string[];
  // HITL (Human-in-the-Loop) approval fields
  hitl?: boolean;
  hitlActions?: string[];
  hitlThreadId?: string;
  hitlIsDeployed?: boolean;
}

interface FilePreview {
  id: string;
  file: File;
  path?: string;
  loading: boolean;
  error: boolean;
}

/* ------------------ COLOR PALETTE ------------------ */

const AGENT_COLORS = [
  "#10a37f", "#ab68ff", "#19c37d", "#ef4146", "#f5a623", "#0ea5e9",
  "#8b5cf6", "#ec4899", "#14b8a6", "#f97316", "#6366f1", "#84cc16",
];

/* ------------------ HELPERS ------------------ */

function mapApiAgents(apiAgents: OrchAgentSummary[]): Agent[] {
  return apiAgents.map((a, i) => ({
    id: a.deploy_id,
    name: a.agent_name,
    description: a.agent_description || "",
    online: true,
    color: AGENT_COLORS[i % AGENT_COLORS.length],
    deploy_id: a.deploy_id,
    agent_id: a.agent_id,
    version_number: a.version_number,
    version_label: a.version_label,
    environment: a.environment,
  }));
}

function inferHitlFromText(text: string): boolean {
  if (!text) return false;
  const normalized = text.toLowerCase();
  return (
    normalized.includes("waiting for human review") &&
    normalized.includes("available actions")
  );
}

function extractHitlActions(text: string): string[] {
  if (!text) return [];
  const out = new Set<string>();

  for (const line of text.split(/\r?\n/)) {
    const m = line.match(/^[\s>*•-]*([A-Za-z][A-Za-z ]*[A-Za-z])\s*$/);
    if (!m?.[1]) continue;
    const action = m[1].trim();
    if (/approve|reject|edit|cancel/i.test(action)) {
      out.add(action);
    }
  }

  // Fallback for inline formats like "Available actions: • Approve • Reject"
  if (out.size === 0) {
    const inline = text.match(/approve|reject|edit|cancel/gi) ?? [];
    for (const action of inline) {
      out.add(action.charAt(0).toUpperCase() + action.slice(1).toLowerCase());
    }
  }

  return Array.from(out);
}

function mapApiMessages(apiMessages: OrchMessageResponse[]): Message[] {
  return apiMessages.map((m) => {
    const props = (m.properties || {}) as Record<string, any>;
    const isHitl = !!props.hitl || inferHitlFromText(m.text || "");
    const parsedActions = Array.isArray(props.actions)
      ? props.actions
      : extractHitlActions(m.text || "");

    return {
      id: m.id,
      sender: m.sender as "user" | "agent" | "system",
      agentName: m.sender === "agent" ? m.sender_name : undefined,
      content: m.text,
      timestamp: m.timestamp
        ? new Date(m.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        : "",
      category: m.category || "message",
      files: m.files && m.files.length > 0 ? m.files : undefined,
      // Restore HITL metadata from persisted properties.
      // Fallback to text inference because some interrupted rows may miss fields.
      hitl: isHitl,
      hitlActions: isHitl ? parsedActions : undefined,
      hitlThreadId: isHitl ? (props.thread_id ?? m.session_id ?? "") : undefined,
      // Orchestrator chat runs deployed agents; default true when missing.
      hitlIsDeployed: isHitl
        ? (props.is_deployed_run !== undefined ? !!props.is_deployed_run : true)
        : undefined,
    };
  });
}

function hitlStatusLabel(value: string): string {
  const normalized = (value || "").toLowerCase();
  if (normalized.includes("reject")) return "Rejected";
  if (normalized.includes("approve")) return "Approved";
  if (normalized.includes("edit")) return "Edited";
  if (normalized.includes("cancel")) return "Cancelled";
  if (normalized.includes("timeout")) return "Timed out";
  return "Resolved";
}

function groupSessionsByDate(
  sessions: OrchSessionSummary[],
  getLabel: (key: string) => string,
): Record<string, OrchSessionSummary[]> {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const weekAgo = new Date(today);
  weekAgo.setDate(weekAgo.getDate() - 7);

  const groups: Record<string, OrchSessionSummary[]> = {};

  for (const s of sessions) {
    const ts = s.last_timestamp ? new Date(s.last_timestamp) : new Date(0);
    let label: string;
    if (ts >= today) label = getLabel("Today");
    else if (ts >= yesterday) label = getLabel("Yesterday");
    else if (ts >= weekAgo) label = getLabel("Previous 7 Days");
    else label = getLabel("Older");

    if (!groups[label]) groups[label] = [];
    groups[label].push(s);
  }
  return groups;
}

/* ------------------ COMPONENT ------------------ */

export default function AgentOrchestrator() {
  const { t } = useTranslation();
  const { permissions } = useContext(AuthContext);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [showMentions, setShowMentions] = useState(false);
  const [filteredAgents, setFilteredAgents] = useState<Agent[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [selectedModelId, setSelectedModelId] = useState("");
  const [showModelPicker, setShowModelPicker] = useState(false);
  const [currentSessionId, setCurrentSessionId] = useState<string>(crypto.randomUUID());
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [isSending, setIsSending] = useState(false);
  const [streamingAgentName, setStreamingAgentName] = useState<string>("");
  const [streamingMsgId, setStreamingMsgId] = useState<string | null>(null);
  // HITL state: track which message had its action clicked
  const [hitlDoneMap, setHitlDoneMap] = useState<Record<string, string>>({});
  const [hitlLoadingId, setHitlLoadingId] = useState<string | null>(null);
  const [hitlLoadingAction, setHitlLoadingAction] = useState<string | null>(null);
  const [uploadFiles, setUploadFiles] = useState<FilePreview[]>([]);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const modelPickerRef = useRef<HTMLDivElement>(null);
  const hitlSessionRef = useRef<string | null>(null);

  /* ------------------ FILE UPLOAD ------------------ */

  const { mutate: uploadFileMutate } = usePostUploadFileV2();
  const ALLOWED_EXTENSIONS = ["png", "jpg", "jpeg"];

  const uploadFile = (file: File) => {
    const ext = file.name.split(".").pop()?.toLowerCase();
    if (!ext || !ALLOWED_EXTENSIONS.includes(ext)) return;

    const id = crypto.randomUUID().slice(0, 10);
    setUploadFiles((prev) => [...prev, { id, file, loading: true, error: false }]);

    uploadFileMutate(
      { file },
      {
        onSuccess: (data: any) => {
          setUploadFiles((prev) =>
            prev.map((f) => (f.id === id ? { ...f, loading: false, path: data.file_path } : f)),
          );
        },
        onError: () => {
          setUploadFiles((prev) =>
            prev.map((f) => (f.id === id ? { ...f, loading: false, error: true } : f)),
          );
        },
      },
    );
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) uploadFile(file);
    e.target.value = "";
  };

  const handlePaste = (e: React.ClipboardEvent) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (let i = 0; i < items.length; i++) {
      if (items[i].type.startsWith("image/")) {
        const blob = items[i].getAsFile();
        if (blob) {
          e.preventDefault();
          uploadFile(blob);
          return;
        }
      }
    }
  };

  const removeFile = (id: string) => {
    setUploadFiles((prev) => prev.filter((f) => f.id !== id));
  };

  /* ------------------ API HOOKS ------------------ */

  const { data: apiAgents } = useGetOrchAgents();
  const { data: apiSessions, refetch: refetchSessions } = useGetOrchSessions();
  const { mutate: deleteSession } = useDeleteOrchSession();

  const agents: Agent[] = useMemo(
    () => (apiAgents ? mapApiAgents(apiAgents) : []),
    [apiAgents],
  );
  const selectedAgent = useMemo(
    () => agents.find((a) => a.id === selectedModelId) || agents[0],
    [agents, selectedModelId],
  );
  const canInteract = permissions?.includes("interact_agents") ?? false;

  // The effective session ID for fetching messages: activeSessionId is set
  // when the user clicks a session in the sidebar.  When null (e.g. after
  // streaming created a new session), fall back to currentSessionId if it
  // exists in the sessions list (meaning it was persisted to the DB).
  const effectiveSessionId = useMemo(() => {
    if (activeSessionId) return activeSessionId;
    if (apiSessions?.some((s) => s.session_id === currentSessionId)) return currentSessionId;
    return null;
  }, [activeSessionId, currentSessionId, apiSessions]);

  // Load messages when switching to an existing session
  const { data: apiSessionMessages, refetch: refetchMessages } = useGetOrchMessages(
    { session_id: effectiveSessionId || "" },
    {
      enabled: !!effectiveSessionId,
      refetchOnWindowFocus: true,
      staleTime: 0,
      // Prevent background polling from clobbering the local streaming placeholder/tokens.
      refetchInterval: isSending ? false : 5000,
    },
  );

  useEffect(() => {
    if (apiSessionMessages && effectiveSessionId) {
      // Keep local in-flight stream state intact for the active session.
      if (isSending && effectiveSessionId === currentSessionId) {
        return;
      }
      const mapped = mapApiMessages(apiSessionMessages);
      setMessages(mapped);
      setCurrentSessionId(effectiveSessionId);
      // Reset HITL UI state only when switching sessions (not on every poll).
      if (hitlSessionRef.current !== effectiveSessionId) {
        setHitlDoneMap({});
        setHitlLoadingId(null);
        hitlSessionRef.current = effectiveSessionId;
      }

      // Sync selected model with the session's active agent
      const sessionInfo = apiSessions?.find((s) => s.session_id === effectiveSessionId);
      if (sessionInfo?.active_agent_name) {
        const activeAgent = agents.find((a) => a.name === sessionInfo.active_agent_name);
        if (activeAgent) {
          setSelectedModelId(activeAgent.id);
        }
      }
    }
  }, [apiSessionMessages, effectiveSessionId, apiSessions, agents]);

  // Keep HITL status in sync when decisions happen on HITL Approvals page.
  // This lets orchestrator chat hide the pending banner and show final status
  // (Approved / Rejected / etc.) without requiring a full page reload.
  useEffect(() => {
    const hitlMsgs = messages.filter((m) => m.hitl && m.hitlThreadId);
    if (hitlMsgs.length === 0) return;

    let isMounted = true;
    const syncStatuses = async () => {
      try {
        const res = await api.get(`${getURL("HITL")}/pending`, {
          params: { status: "all" },
        });
        const rows: Array<{ thread_id?: string; status?: string; requested_at?: string }> = Array.isArray(res.data)
          ? res.data
          : [];

        // Build per-thread request timelines (oldest -> newest).
        const reqByThread = new Map<string, Array<{ status: string; requestedAt: number }>>();
        for (const row of rows) {
          if (!row?.thread_id || !row?.status) continue;
          const list = reqByThread.get(row.thread_id) ?? [];
          list.push({
            status: row.status,
            requestedAt: row.requested_at ? Date.parse(row.requested_at) : 0,
          });
          reqByThread.set(row.thread_id, list);
        }
        for (const list of reqByThread.values()) {
          list.sort((a, b) => a.requestedAt - b.requestedAt);
        }

        // Build per-thread HITL message timelines in chat order.
        const msgByThread = new Map<string, Message[]>();
        for (const msg of hitlMsgs) {
          const threadId = msg.hitlThreadId ?? "";
          const list = msgByThread.get(threadId) ?? [];
          list.push(msg);
          msgByThread.set(threadId, list);
        }

        // Assign status to each HITL message by timeline index in the same thread.
        const nextMap: Record<string, string> = {};
        for (const [threadId, threadMsgs] of msgByThread.entries()) {
          const threadReqs = reqByThread.get(threadId) ?? [];
          for (let i = 0; i < threadMsgs.length; i++) {
            const req = threadReqs[i];
            if (!req) continue;
            if (req.status.toLowerCase() !== "pending") {
              nextMap[threadMsgs[i].id] = hitlStatusLabel(req.status);
            }
          }
        }

        if (!isMounted) return;
        setHitlDoneMap(nextMap);
      } catch {
        // Best-effort status sync only; keep existing UI if polling fails.
      }
    };

    syncStatuses();
    const timer = window.setInterval(syncStatuses, 4000);
    return () => {
      isMounted = false;
      window.clearInterval(timer);
    };
  }, [messages]);

  // Set default selected model when agents load
  useEffect(() => {
    if (agents.length > 0 && !selectedModelId) {
      setSelectedModelId(agents[0].id);
    }
  }, [agents, selectedModelId]);

  // Update filteredAgents when agents load
  useEffect(() => {
    setFilteredAgents(agents);
  }, [agents]);

  useEffect(() => {
    // Use instant scroll while streaming so it keeps up with fast tokens;
    // smooth scroll otherwise for a nicer UX.
    messagesEndRef.current?.scrollIntoView({
      behavior: isSending ? "auto" : "smooth",
    });
  }, [messages, isSending, streamingAgentName]);

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (modelPickerRef.current && !modelPickerRef.current.contains(e.target as Node)) {
        setShowModelPicker(false);
      }
    };
    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  /* ------------------ HELPERS ------------------ */

  const timeNow = () =>
    new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const highlightMentions = (text: string) => {
    // Build a regex that matches any known @agent_name (including spaces)
    // so "@smart agent" is bolded as one unit, not just "@smart".
    if (agents.length === 0) return [text];
    const escaped = [...agents]
      .sort((a, b) => b.name.length - a.name.length)
      .map((a) => a.name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    const pattern = new RegExp(`(@(?:${escaped.join("|")}))`, "gi");
    return text.split(pattern).map((part, i) =>
      part.startsWith("@") ? (
        <span key={i} className="font-semibold text-primary">
          {part}
        </span>
      ) : (
        part
      )
    );
  };

  const getAgentColor = (name?: string) => {
    const agent = agents.find((a) => a.name === name);
    return agent?.color || "#10a37f";
  };

  const versionBadge = (versionLabel: string) => (
    <span className="ml-2 inline-flex items-center rounded-full border border-border bg-muted px-1.5 py-0.5 text-xxs font-semibold uppercase leading-none text-muted-foreground">
      {versionLabel}
    </span>
  );

  const uatBadge = (environment: string) =>
    String(environment).toLowerCase() === "uat" ? (
      <span className="ml-1.5 inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-xxs font-semibold uppercase leading-none text-amber-700">
        UAT
      </span>
    ) : null;

  /* ------------------ HITL ACTION HANDLER ------------------ */

  const handleHitlAction = useCallback(
    async (msgId: string, threadId: string, action: string) => {
      if (hitlDoneMap[msgId] || hitlLoadingId) return;
      setHitlLoadingId(msgId);
      setHitlLoadingAction(action);
      try {
        const res = await api.post(`${getURL("HITL")}/${threadId}/resume`, {
          action,
          feedback: "",
          edited_value: "",
        });
        setHitlDoneMap((prev) => ({ ...prev, [msgId]: action }));

        const resData = res.data;

        if (resData?.status === "interrupted" && resData.interrupt_data) {
          // Another HITL node was hit downstream — show new approval message
          const newInterrupt = resData.interrupt_data;
          const question = newInterrupt.question || "Approval required";
          const newActions: string[] = newInterrupt.actions || [];
          setMessages((prev) => [
            ...prev,
            {
              id: crypto.randomUUID(),
              sender: "agent",
              agentName: prev.find((m) => m.id === msgId)?.agentName,
              content: question,
              timestamp: timeNow(),
              hitl: true,
              hitlActions: newActions,
              hitlThreadId: threadId,
            },
          ]);
        } else if (resData?.status === "completed") {
          // Graph finished — show only resumed AI output in orchestrator chat.
          setMessages((prev) => {
            const name = prev.find((m) => m.id === msgId)?.agentName;
            if (!resData.output_text) return prev;
            return [
              ...prev,
              {
                id: crypto.randomUUID(),
                sender: "agent" as const,
                agentName: name,
                content: resData.output_text,
                timestamp: timeNow(),
              },
            ];
          });
          // Refetch messages from DB so the persisted orch_conversation
          // response is available if user reloads or navigates away.
          refetchMessages();
        }
      } catch (_err) {
        // leave buttons enabled so user can retry
      } finally {
        setHitlLoadingId(null);
        setHitlLoadingAction(null);
      }
    },
    [hitlDoneMap, hitlLoadingId, timeNow, refetchMessages],
  );

  /* ------------------ INPUT HANDLING ------------------ */

  const handleInputChange = (value: string) => {
    setInput(value);
    const match = value.match(/@([\w\s]*)$/);
    if (match) {
      const query = match[1].toLowerCase();
      setFilteredAgents(agents.filter((a) => a.name.toLowerCase().includes(query)));
      setShowMentions(true);
    } else {
      setShowMentions(false);
    }
  };

  const handleSelectAgent = (agent: Agent) => {
    const updated = input.replace(/@[\w\s]*$/, `@${agent.name} `);
    setInput(updated);
    setSelectedModelId(agent.id);
    setShowMentions(false);
    textareaRef.current?.focus();
  };

  /* ------------------ SEND MESSAGE ------------------ */

  const handleSend = useCallback(async () => {
    const hasFiles = uploadFiles.some((f) => f.path && !f.loading && !f.error);
    if (!canInteract || (!input.trim() && !hasFiles) || isSending || agents.length === 0) return;

    // Collect uploaded file paths and clear previews
    const filePaths = uploadFiles
      .filter((f) => f.path && !f.loading && !f.error)
      .map((f) => f.path!);
    setUploadFiles([]);

    // Detect explicit @mention vs implicit (sticky) routing.
    // Sort by name length descending so "rag agent_new" matches before "rag agent".
    const explicitAgent = [...agents]
      .sort((a, b) => b.name.length - a.name.length)
      .find((a) => input.includes(`@${a.name}`));
    const fallbackAgent = selectedAgent || agents[0];

    // If user explicitly @mentioned an agent, update the selected model (sticky switch)
    if (explicitAgent && explicitAgent.id !== selectedModelId) {
      setSelectedModelId(explicitAgent.id);
    }

    // Target agent: explicit @mention wins, otherwise use sticky (selectedModel)
    const targetAgent = explicitAgent || fallbackAgent;

    // Strip the @agent_name mention so the agent only receives the actual question
    const cleanedInput = explicitAgent
      ? input.replace(new RegExp(`@${explicitAgent.name}\\s*`, "g"), "").trim()
      : input.trim();

    // Agent message placeholder — created upfront so "Thinking..." shows inside the bubble
    const agentMsgId = crypto.randomUUID();
    setStreamingMsgId(agentMsgId);

    // Add both user message AND agent "thinking" placeholder.
    // flushSync commits the DOM update synchronously, then we await a
    // double-rAF to guarantee the browser has actually painted the
    // "Thinking..." indicator before the network request begins.
    const userMessage: Message = {
      id: crypto.randomUUID(),
      sender: "user",
      content: input,
      timestamp: timeNow(),
      files: filePaths.length > 0 ? filePaths : undefined,
    };
    flushSync(() => {
      setMessages((prev) => [
        ...prev,
        userMessage,
        {
          id: agentMsgId,
          sender: "agent" as const,
          agentName: targetAgent.name,
          content: "",  // empty = "Thinking..." state
          timestamp: timeNow(),
        },
      ]);
      setInput("");
      setShowMentions(false);
      setIsSending(true);
      setStreamingAgentName(targetAgent.name);
    });

    // Wait for the browser to actually paint the thinking state.
    // Double-rAF: first rAF fires before paint, second fires after paint.
    await new Promise<void>((resolve) =>
      requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
    );

    let accumulated = "";
    let rafHandle: number | null = null;
    let pendingContent: string | null = null;
    let hitlPauseReceived = false;
    let receivedToken = false;
    let latestAgentAddMessageText = "";

    // Flush the latest accumulated content to React state.
    // Called inside a rAF so we update at most once per frame (~60fps),
    // keeping the UI responsive while still showing progressive tokens.
    const flushToReact = () => {
      rafHandle = null;
      if (pendingContent === null) return;
      const content = pendingContent;
      pendingContent = null;
      flushSync(() => {
        setStreamingAgentName("");
        setMessages((prev) =>
          prev.map((m) =>
            m.id === agentMsgId ? { ...m, content } : m,
          ),
        );
      });
    };

    // Helper: update the agent message bubble content.
    // Tokens arrive very rapidly; we accumulate them and schedule
    // a single React update per animation frame to stay smooth.
    const updateAgentMsg = (content: string, immediate = false) => {
      accumulated = content;
      if (immediate) {
        // For final/error updates, flush synchronously
        if (rafHandle !== null) { cancelAnimationFrame(rafHandle); rafHandle = null; }
        pendingContent = null;
        flushSync(() => {
          setStreamingAgentName("");
          setMessages((prev) =>
            prev.map((m) =>
              m.id === agentMsgId ? { ...m, content } : m,
            ),
          );
        });
        return;
      }
      pendingContent = content;
      if (rafHandle === null) {
        rafHandle = requestAnimationFrame(flushToReact);
      }
    };

    // Always send agent_id — explicit @mention or sticky selectedModel.
    // Backend sticky routing acts as fallback if agent_id is somehow missing.
    const requestBody: any = {
      session_id: currentSessionId,
      agent_id: targetAgent.agent_id,
      deployment_id: targetAgent.deploy_id,
      input_value: cleanedInput,
      version_number: targetAgent.version_number,
    };
    if (filePaths.length > 0) {
      requestBody.files = filePaths;
    }

    const buildController = new AbortController();

    try {
      await performStreamingRequest({
        method: "POST",
        url: `${getURL("ORCHESTRATOR")}/chat/stream`,
        body: requestBody,
        buildController,
        onData: async (event: any) => {
          const eventType: string = event?.event;
          const data: any = event?.data;

          const isHitlEvent =
            eventType === "add_message" &&
            (
              !!data?.properties?.hitl ||
              inferHitlFromText(String(data?.text || data?.message || ""))
            );

          if (isHitlEvent) {
            // HITL pause event — update agent message with HITL metadata
            // so the UI renders approval action buttons.
            hitlPauseReceived = true;
            const actions: string[] = Array.isArray(data?.properties?.actions)
              ? data.properties.actions
              : extractHitlActions(String(data?.text || data?.message || ""));
            const threadId: string = data?.properties?.thread_id ?? currentSessionId ?? "";
            const hitlText: string = data.text || data.message || "";
            const isDeployedRun: boolean = data?.properties?.is_deployed_run ?? true;
            flushSync(() => {
              setStreamingAgentName("");
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === agentMsgId
                    ? {
                        ...m,
                        content: hitlText,
                        hitl: true,
                        hitlActions: actions,
                        hitlThreadId: threadId,
                        hitlIsDeployed: isDeployedRun,
                      }
                    : m,
                ),
              );
            });
            // Don't return false — let the stream continue to consume
            // remaining events (end_vertex, end).
          } else if (eventType === "add_message" && data?.content_blocks?.length) {
            // Only show content_blocks that contain actual tool calls.
            // Each flow node (Chat Input, Worker Node, Chat Output) sends its
            // own add_message event; pipeline nodes only carry plain text steps
            // which would appear as duplicate Input/Output entries. Filtering
            // to tool_use blocks means we only show meaningful agent reasoning.
            const toolBlocks = data.content_blocks.filter((block: any) =>
              block.contents?.some((c: any) => c.type === "tool_use"),
            );
            if (toolBlocks.length > 0) {
              flushSync(() => {
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === agentMsgId
                      ? {
                          ...m,
                          // Replace (not append) — each add_message is a
                          // progressive update of the same Worker Node message
                          // (Accessing → Executed), not a new block.
                          contentBlocks: toolBlocks,
                          blocksState: "partial",
                        }
                      : m,
                  ),
                );
              });
            }
          } else if (eventType === "add_message" && (data?.text || data?.message) && !hitlPauseReceived) {
            // Keep add_message text as a fallback, but don't immediately overwrite
            // the thinking bubble. Some graphs emit user/input-node add_message
            // events before AI tokens; rendering those here causes echo + no stream UX.
            const sender = String(data?.sender || data?.sender_name || "").toLowerCase();
            const isUserMessage = sender.includes("user");
            const addMessageText = String(data.text || data.message || "");
            if (!isUserMessage && addMessageText.trim()) {
              latestAgentAddMessageText = addMessageText;
            }
          } else if (eventType === "token" && data?.chunk) {
            // Progressive streaming — append each token chunk (throttled)
            receivedToken = true;
            accumulated += data.chunk;
            updateAgentMsg(accumulated);
          } else if (eventType === "error") {
            updateAgentMsg(data?.text || "An error occurred", true);
            return false;
          } else if (eventType === "end") {
            // End event carries the final complete text — flush immediately.
            // BUT: if we received a HITL pause, do NOT overwrite the HITL
            // message with agent_text — the action buttons must stay visible.
            if (data?.agent_text && !hitlPauseReceived) {
              updateAgentMsg(data.agent_text, true);
            } else if (!hitlPauseReceived && !receivedToken && latestAgentAddMessageText.trim()) {
              // Fallback for non-token flows where response text came only via
              // add_message and end has no agent_text payload.
              updateAgentMsg(latestAgentAddMessageText, true);
            }
            // Mark content blocks as fully finished
            if (!hitlPauseReceived) {
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === agentMsgId && m.contentBlocks?.length
                    ? { ...m, blocksState: "complete" }
                    : m,
                ),
              );
            }
            refetchSessions();
            // Force a fast message sync for existing sessions so local streamed
            // content is not replaced by stale polled data.
            if (effectiveSessionId) {
              refetchMessages();
            }
            return false;
          }
          return true;
        },
        onError: (statusCode) => {
          updateAgentMsg(`Error: server returned ${statusCode}`, true);
        },
        onNetworkError: (error) => {
          if (error.name !== "AbortError") {
            updateAgentMsg("Sorry, something went wrong. Please try again.", true);
          }
        },
      });
    } catch {
      if (!accumulated) {
        updateAgentMsg("Sorry, something went wrong. Please try again.", true);
      }
    } finally {
      // Flush any remaining buffered content and clean up
      if (rafHandle !== null) { cancelAnimationFrame(rafHandle); rafHandle = null; }
      if (pendingContent !== null) {
        const finalContent = pendingContent;
        pendingContent = null;
        setMessages((prev) =>
          prev.map((m) =>
            m.id === agentMsgId ? { ...m, content: finalContent } : m,
          ),
        );
      } else if (!hitlPauseReceived && !receivedToken && latestAgentAddMessageText.trim()) {
        // Defensive fallback if stream closes before we get a parsable end event.
        setMessages((prev) =>
          prev.map((m) =>
            m.id === agentMsgId ? { ...m, content: latestAgentAddMessageText } : m,
          ),
        );
      }
      setIsSending(false);
      setStreamingAgentName("");
      setStreamingMsgId(null);
    }
  }, [canInteract, input, isSending, agents, selectedAgent, selectedModelId, currentSessionId, effectiveSessionId, refetchSessions, refetchMessages]);

  /* ------------------ SESSION MANAGEMENT ------------------ */

  const handleNewChat = () => {
    setCurrentSessionId(crypto.randomUUID());
    setActiveSessionId(null);
    setMessages([]);
  };

  const handleSelectSession = (sessionId: string) => {
    setActiveSessionId(sessionId);
  };

  const handleDeleteSession = (sessionId: string) => {
    deleteSession(
      { session_id: sessionId },
      {
        onSuccess: () => {
          if (currentSessionId === sessionId) {
            handleNewChat();
          }
          refetchSessions();
        },
      },
    );
  };

  /* ---- group chat history by date ---- */
  const grouped = useMemo(
    () => groupSessionsByDate(apiSessions || [], t),
    [apiSessions, t],
  );

  /* ------------------ RENDER ------------------ */

  return (
    <div className="flex h-screen w-full overflow-hidden bg-background text-foreground">
      {/* ================ SIDEBAR ================ */}
      <div
        className={`flex flex-col overflow-hidden border-r border-border bg-muted transition-all duration-200 ${
          sidebarOpen ? "w-64 min-w-[16rem]" : "w-0 min-w-0"
        }`}
      >
        {/* Sidebar Header */}
        <div className="flex items-center justify-between p-3">
          <button
            onClick={() => setSidebarOpen(false)}
            className="flex items-center rounded-md p-1.5 text-muted-foreground hover:bg-accent"
          >
            <PanelLeftClose size={18} />
          </button>
          <button
            onClick={handleNewChat}
            className="flex items-center rounded-md p-1.5 text-muted-foreground hover:bg-accent"
          >
            <Plus size={18} />
          </button>
        </div>

        {/* Chat History */}
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="shrink-0 px-4 pb-1 pt-2 text-xxs font-semibold uppercase tracking-wide text-muted-foreground">
            {t("Conversations")}
          </div>
          <div className="flex-1 overflow-y-auto scroll-smooth px-2" style={{ scrollbarWidth: "thin" }}>
            {Object.entries(grouped).map(([date, chats]) => (
              <div key={date} className="mb-4">
                <div className="px-2 pb-1 pt-2 text-xxs font-semibold uppercase tracking-wide text-muted-foreground">
                  {date}
                </div>
                {chats.map((chat) => (
                  <div
                    key={chat.session_id}
                    className="group relative flex items-center"
                  >
                    <button
                      onClick={() => handleSelectSession(chat.session_id)}
                      className={`flex min-w-0 flex-1 items-center gap-2 truncate rounded-lg px-2 py-2.5 pr-8 text-left text-sm text-foreground hover:bg-accent ${
                        currentSessionId === chat.session_id ? "bg-accent" : ""
                      }`}
                    >
                      <MessageSquare size={14} className="shrink-0 opacity-50" />
                      <span className="truncate">{chat.preview || t("New conversation")}</span>
                    </button>
                    {/* Delete button — visible on hover */}
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        handleDeleteSession(chat.session_id);
                      }}
                      className="invisible absolute right-1 shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-red-500 group-hover:visible"
                      title={t("Delete session")}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>

        {/* Agents Panel */}
        <div className="flex min-h-0 flex-1 flex-col border-t border-border">
          <div className="shrink-0 px-4 pb-2 pt-3 text-xxs font-semibold uppercase tracking-wide text-muted-foreground">
            {t("Agents")}
          </div>
          <div className="flex-1 overflow-y-auto scroll-smooth px-2 pb-2" style={{ scrollbarWidth: "thin" }}>
            <div className="flex flex-col gap-0.5">
              {agents.map((agent) => (
                <button
                  key={agent.id}
                  onClick={() => {
                    setSelectedModelId(agent.id);
                    setShowModelPicker(false);
                  }}
                  className={`flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-[13px] text-foreground hover:bg-accent ${
                    selectedModelId === agent.id ? "bg-accent" : ""
                  }`}
                >
                  <span
                    className="h-2 w-2 shrink-0 rounded-full"
                    style={{ background: agent.online ? agent.color : undefined }}
                  />
                  <span className="flex min-w-0 items-center">
                    <span className="truncate">{agent.name}</span>
                    {versionBadge(agent.version_label)}
                    {uatBadge(agent.environment)}
                  </span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* ================ MAIN AREA ================ */}
      <div className="relative flex flex-1 flex-col">
        {/* Top Bar */}
        <div className="flex h-[52px] shrink-0 items-center gap-2 border-b border-border px-4">
          {!sidebarOpen && (
            <button
              onClick={() => setSidebarOpen(true)}
              className="flex items-center rounded-md p-1.5 text-muted-foreground hover:bg-accent"
            >
              <PanelLeft size={18} />
            </button>
          )}

          {/* Model selector */}
          <div ref={modelPickerRef} className="relative">
            <button
              onClick={() => setShowModelPicker(!showModelPicker)}
              className="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[15px] font-semibold text-foreground hover:bg-accent"
            >
              <Sparkles size={16} style={{ color: selectedAgent?.color || "#10a37f" }} />
              {selectedAgent ? (
                <span className="flex items-center">
                  <span>{selectedAgent.name}</span>
                  {versionBadge(selectedAgent.version_label)}
                  {uatBadge(selectedAgent.environment)}
                </span>
              ) : (
                t("Select Agent")
              )}
              <ChevronDown size={14} className="opacity-50" />
            </button>

            {showModelPicker && (
              <div className="absolute left-0 top-full z-50 mt-1 min-w-[240px] rounded-xl border border-border bg-popover p-1 shadow-lg">
                {agents.map((agent) => (
                  <button
                    key={agent.id}
                    onClick={() => {
                      setSelectedModelId(agent.id);
                      setShowModelPicker(false);
                    }}
                    className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-2.5 text-left text-sm text-foreground hover:bg-accent ${
                      selectedModelId === agent.id ? "bg-accent" : ""
                    }`}
                  >
                    <span
                      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md"
                      style={{ background: agent.color }}
                    >
                      <Sparkles size={14} color="white" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center font-medium">
                        <span>{agent.name}</span>
                        {versionBadge(agent.version_label)}
                        {uatBadge(agent.environment)}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {agent.description}
                      </div>
                    </div>
                    {selectedModelId === agent.id && (
                      <span className="ml-auto text-primary">
                        <Check size={14} />
                      </span>
                    )}
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* ================ MESSAGES ================ */}
        <div className="flex flex-1 flex-col items-center overflow-y-auto">
          <div className="w-full max-w-3xl px-6 pb-44 pt-6">
            {messages.map((msg, idx) => {
              // Context reset divider
              if (msg.category === "context_reset") {
                return (
                  <div key={msg.id} className="flex items-center gap-3 py-4">
                    <div className="h-px flex-1 bg-border" />
                    <span className="text-xs font-medium text-muted-foreground">
                      {msg.content}
                    </span>
                    <div className="h-px flex-1 bg-border" />
                  </div>
                );
              }

              const isUser = msg.sender === "user";
              const isThinking = msg.sender === "agent" && msg.content === "" && isSending;
              const hasFollowupAgentReply = messages
                .slice(idx + 1)
                .some(
                  (nextMsg) =>
                    nextMsg.sender === "agent" &&
                    !nextMsg.hitl &&
                    (!!nextMsg.content?.trim() || !!nextMsg.contentBlocks?.length),
                );
              const explicitHitlStatus = hitlDoneMap[msg.id];
              const hitlResolved = msg.hitlIsDeployed
                ? !!explicitHitlStatus
                : (!!explicitHitlStatus || hasFollowupAgentReply);
              const resolvedLabel = explicitHitlStatus || (!msg.hitlIsDeployed && hasFollowupAgentReply ? "Completed" : "");
              const isRejectedResolution = resolvedLabel.toLowerCase().includes("reject");
              return (
                <div key={msg.id} className="flex items-start gap-4 py-5">
                  {/* Avatar */}
                  <div
                    className={`mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center ${
                      isUser ? "rounded-full bg-muted" : "rounded-lg"
                    }`}
                    style={!isUser ? { background: getAgentColor(msg.agentName) } : undefined}
                  >
                    {isUser ? (
                      <User size={16} className="text-muted-foreground" />
                    ) : (
                      <Sparkles size={16} color="white" />
                    )}
                  </div>

                  {/* Content */}
                  <div className="min-w-0 flex-1">
                    <div className="mb-1 flex items-center gap-2 text-sm font-semibold text-foreground">
                      {isUser ? t("You") : msg.agentName}
                      <span className="text-xs font-normal text-muted-foreground">
                        {msg.timestamp}
                      </span>
                    </div>
                    {isThinking ? (
                      <div className="flex items-center gap-2">
                        <Loader2 size={16} className="animate-spin text-muted-foreground" />
                        <span className="text-sm text-muted-foreground">{t("Thinking...")}</span>
                      </div>
                    ) : isUser ? (
                      <div className="text-[15px] leading-relaxed text-foreground/80">
                        {highlightMentions(msg.content)}
                        {msg.files && msg.files.length > 0 && (
                          <div className="mt-2 flex flex-wrap gap-2">
                            {msg.files.map((filePath, idx) => (
                              <img
                                key={idx}
                                src={`${BASE_URL_API}files/images/${filePath}`}
                                alt="uploaded"
                                className="max-h-48 max-w-xs rounded-lg border border-border object-contain"
                              />
                            ))}
                          </div>
                        )}
                      </div>
                    ) : (
                      <div className="text-[15px] leading-relaxed text-foreground/80">
                        {msg.contentBlocks && msg.contentBlocks.length > 0 && (
                          <ContentBlockDisplay
                            contentBlocks={msg.contentBlocks}
                            chatId={msg.id}
                            state={msg.blocksState}
                            isLoading={isSending && msg.id === streamingMsgId}
                          />
                        )}
                        <MarkdownField
                          chat={{}}
                          isEmpty={!msg.content}
                          chatMessage={msg.content}
                          editedFlag={null}
                        />
                        {/* HITL action buttons */}
                        {msg.hitl && (
                          msg.hitlIsDeployed ? (
                            /* Deployed runs: approval goes to dept admin via HITL page */
                            hitlResolved ? (
                              <div
                                className={[
                                  "mt-3 flex items-center gap-2 rounded-md border px-4 py-2.5 text-sm",
                                  isRejectedResolution
                                    ? "border-red-300 bg-red-50 text-red-700 dark:border-red-700 dark:bg-red-950/30 dark:text-red-300"
                                    : "border-green-300 bg-green-50 text-green-700 dark:border-green-700 dark:bg-green-950/30 dark:text-green-300",
                                ].join(" ")}
                              >
                                <span className="font-medium">Human review status:</span>
                                <span>{resolvedLabel}</span>
                              </div>
                            ) : (
                              <div className="mt-3 flex items-center gap-2 rounded-md border border-amber-300 bg-amber-50 px-4 py-2.5 text-sm dark:border-amber-700 dark:bg-amber-950/30">
                                <Clock size={16} className="shrink-0 text-amber-600 dark:text-amber-400" />
                                <span className="text-amber-700 dark:text-amber-300">
                                  Pending department admin approval. The assigned admin can approve or reject from the{" "}
                                  <a
                                    href="/hitl-approvals"
                                    className="font-medium underline hover:text-amber-900 dark:hover:text-amber-100"
                                  >
                                    HITL Approvals
                                  </a>{" "}
                                  page.
                                </span>
                              </div>
                            )
                          ) : (
                          (msg.hitlActions && msg.hitlActions.length > 0) ? (
                          <div className="mt-3 flex flex-col gap-2.5">
                            <div className="flex flex-wrap gap-2">
                            {msg.hitlActions.map((action) => {
                              const done = hitlDoneMap[msg.id];
                              const isLoading = hitlLoadingId === msg.id;
                              const isThisAction = hitlLoadingAction === action;
                              const isReject = action.toLowerCase().includes("reject");
                              return (
                                <button
                                  key={action}
                                  onClick={() =>
                                    handleHitlAction(msg.id, msg.hitlThreadId ?? "", action)
                                  }
                                  disabled={!!done || isLoading}
                                  className={[
                                    "inline-flex items-center gap-1.5 rounded-md border px-4 py-1.5 text-sm font-medium transition-colors",
                                    done === action
                                      ? isReject
                                        ? "border-red-500 bg-red-50 text-red-700 dark:bg-red-900/20 dark:text-red-400"
                                        : "border-green-500 bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-400"
                                      : done
                                        ? "cursor-not-allowed border-border bg-muted/30 text-muted-foreground opacity-50"
                                        : isLoading && isThisAction
                                          ? isReject
                                            ? "cursor-wait border-red-400 bg-red-50 text-red-600 dark:bg-red-900/20 dark:text-red-400"
                                            : "cursor-wait border-green-400 bg-green-50 text-green-600 dark:bg-green-900/20 dark:text-green-400"
                                          : isLoading
                                            ? "cursor-not-allowed border-border bg-muted/30 text-muted-foreground opacity-50"
                                            : isReject
                                              ? "cursor-pointer border-red-300 text-red-600 hover:bg-red-50 dark:border-red-700 dark:text-red-400 dark:hover:bg-red-950/30"
                                              : "cursor-pointer border-border text-foreground hover:bg-muted",
                                  ].join(" ")}
                                >
                                  {isLoading && isThisAction && (
                                    <Loader2 size={14} className="animate-spin" />
                                  )}
                                  {isLoading && isThisAction
                                    ? "Submitting..."
                                    : done === action
                                      ? `\u2713 ${action}`
                                      : action}
                                </button>
                              );
                            })}
                            </div>
                            {hitlDoneMap[msg.id] && (
                              <span className="text-xs text-muted-foreground">
                                Decision submitted — agent continued.
                              </span>
                            )}
                          </div>
                          ) : null
                          )
                        )}
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
            <div ref={messagesEndRef} />
          </div>
        </div>

        {/* ================ INPUT AREA ================ */}
        <div className="pointer-events-none absolute bottom-0 left-0 right-0 flex justify-center bg-gradient-to-t from-background from-40% to-transparent px-6 pb-6">
          <div className="pointer-events-auto relative w-full max-w-3xl">
            {/* Mention dropdown */}
            {showMentions && (
              <div className="absolute bottom-full left-0 z-50 mb-2 max-h-64 min-w-[240px] overflow-y-auto rounded-xl border border-border bg-popover p-1 shadow-lg">
                {filteredAgents.map((agent) => (
                  <button
                    key={agent.id}
                    onClick={() => handleSelectAgent(agent)}
                    className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2.5 text-left text-sm text-foreground hover:bg-accent"
                  >
                    <span
                      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md"
                      style={{ background: agent.color }}
                    >
                      <Sparkles size={12} color="white" />
                    </span>
                    <div className="min-w-0">
                      <div className="flex items-center font-medium">
                        <span>@{agent.name}</span>
                        {versionBadge(agent.version_label)}
                        {uatBadge(agent.environment)}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {agent.description}
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            )}

            {/* Text Input */}
            <div className="overflow-hidden rounded-2xl border border-border bg-card shadow-sm">
              {/* File previews */}
              {uploadFiles.length > 0 && (
                <div className="flex flex-wrap gap-2 px-4 pt-3">
                  {uploadFiles.map((f) => (
                    <div
                      key={f.id}
                      className="relative flex items-center gap-1.5 rounded-lg border border-border bg-muted px-2.5 py-1.5 text-xs"
                    >
                      {f.loading ? (
                        <Loader2 size={14} className="animate-spin text-muted-foreground" />
                      ) : f.error ? (
                        <span className="text-destructive">Failed</span>
                      ) : (
                        <ImagePlus size={14} className="text-muted-foreground" />
                      )}
                      <span className="max-w-[120px] truncate">{f.file.name}</span>
                      <button
                        onClick={() => removeFile(f.id)}
                        className="ml-0.5 rounded-full p-0.5 text-muted-foreground hover:bg-background hover:text-foreground"
                      >
                        <X size={12} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <textarea
                ref={textareaRef}
                value={input}
                onChange={(e) => handleInputChange(e.target.value)}
                onPaste={handlePaste}
                disabled={isSending || !canInteract}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleSend();
                  }
                }}
                placeholder={
                  !canInteract
                    ? t("You do not have permission to interact with agents.")
                    : isSending
                      ? t("Waiting for response...")
                      : t("Message agents or type @ to mention...")
                }
                rows={1}
                className={`w-full resize-none border-none bg-transparent px-5 py-4 pr-14 text-[15px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-0 ${(isSending || !canInteract) ? "cursor-not-allowed opacity-50" : ""}`}
              />
              <div className="flex items-center justify-between px-3 pb-3">
                <button
                  onClick={() => fileInputRef.current?.click()}
                  disabled={isSending || !canInteract}
                  className={`flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground transition-colors ${(isSending || !canInteract) ? "cursor-not-allowed opacity-50" : "hover:bg-accent hover:text-foreground"}`}
                  title={t("Upload image")}
                >
                  <ImagePlus size={16} />
                </button>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".png,.jpg,.jpeg"
                  className="hidden"
                  onChange={handleFileChange}
                />
                <button
                  onClick={handleSend}
                  disabled={(!input.trim() && !uploadFiles.some((f) => f.path)) || isSending || !canInteract}
                  className={`flex h-8 w-8 items-center justify-center rounded-lg transition-colors ${
                    (input.trim() || uploadFiles.some((f) => f.path)) && !isSending && canInteract
                      ? "bg-foreground text-background hover:opacity-90"
                      : "bg-muted text-muted-foreground"
                  }`}
                >
                  <Send size={16} className="-ml-px -mt-px" />
                </button>
              </div>
            </div>

            <div className="mt-2 text-center text-xs text-muted-foreground">
              {t("Agents can make mistakes. Review important info.")}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
