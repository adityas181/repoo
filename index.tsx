import { useContext, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import IconComponent from "@/components/common/genericIconComponent";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import useAlertStore from "@/stores/alertStore";
import type { HITLRequestItem } from "@/controllers/API/queries/hitl/use-get-hitl-pending";
import { useGetHitlPending } from "@/controllers/API/queries/hitl/use-get-hitl-pending";
import { useResumeHitl } from "@/controllers/API/queries/hitl/use-resume-hitl";
import { useCancelHitl } from "@/controllers/API/queries/hitl/use-cancel-hitl";
import { useDelegateHitl } from "@/controllers/API/queries/hitl/use-delegate-hitl";
import { useGetDelegatableUsers } from "@/controllers/API/queries/hitl/use-get-delegatable-users";
import { AuthContext } from "@/contexts/authContext";

type StatusFilter = "all" | "pending" | "approved" | "rejected" | "cancelled";

const STATUS_COLORS: Record<string, string> = {
  pending:
    "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-400",
  approved:
    "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400",
  rejected: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-400",
  edited: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-400",
  cancelled:
    "bg-gray-100 text-gray-600 dark:bg-gray-800/30 dark:text-gray-400",
  timed_out:
    "bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-400",
};

function formatRelativeTime(isoString: string, t: (key: string, options?: Record<string, unknown>) => string): string {
  const date = new Date(isoString);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return t("{{count}}s ago", { count: diffSec });
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return t("{{count}}m ago", { count: diffMin });
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return t("{{count}}h ago", { count: diffHr });
  const diffDay = Math.floor(diffHr / 24);
  return t("{{count}}d ago", { count: diffDay });
}

function StatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  const colorClass = STATUS_COLORS[status] ?? STATUS_COLORS.pending;
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium capitalize ${colorClass}`}
    >
      {t(status.replace("_", " "))}
    </span>
  );
}

function ConfidenceBadge({ confidence }: { confidence: number }) {
  const color =
    confidence >= 80
      ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
      : confidence >= 40
        ? "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400"
        : "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400";
  return (
    <span
      className={`ml-1.5 inline-flex items-center rounded-full px-1.5 py-0.5 text-xxs font-semibold ${color}`}
    >
      {confidence}%
    </span>
  );
}

function ConfidenceBar({ confidence }: { confidence: number }) {
  const barColor =
    confidence >= 80
      ? "bg-green-500"
      : confidence >= 40
        ? "bg-amber-500"
        : "bg-red-500";
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 w-full max-w-[120px] rounded-full bg-muted">
        <div
          className={`h-2 rounded-full transition-all ${barColor}`}
          style={{ width: `${Math.min(100, Math.max(0, confidence))}%` }}
        />
      </div>
      <span className="text-xs font-medium text-muted-foreground">
        {confidence}%
      </span>
    </div>
  );
}

interface DetailModalProps {
  item: HITLRequestItem | null;
  open: boolean;
  onClose: () => void;
  onAction: (threadId: string, action: string, feedback: string) => void;
  onCancel: (threadId: string) => void;
  onDelegate: (threadId: string, userId: string) => void;
  isActing: boolean;
  canApprove: boolean;
  canReject: boolean;
  isAssignee: boolean;
  currentUserId: string | undefined;
}

function DetailModal({
  item,
  open,
  onClose,
  onAction,
  onCancel,
  onDelegate,
  isActing,
  canApprove,
  canReject,
  isAssignee,
  currentUserId,
}: DetailModalProps) {
  const { t } = useTranslation();
  const [feedback, setFeedback] = useState("");
  const [selectedAction, setSelectedAction] = useState<string | null>(null);
  const [showDelegateUI, setShowDelegateUI] = useState(false);
  const [selectedDelegateUser, setSelectedDelegateUser] = useState("");

  // Fetch delegatable users when delegation UI is open
  const { data: delegatableUsers = [] } = useGetDelegatableUsers(
    { dept_id: showDelegateUI && item?.dept_id ? item.dept_id : null },
    { enabled: showDelegateUI && !!item?.dept_id },
  );

  if (!item) return null;

  const actions = item.interrupt_data?.actions ?? [];
  const question = item.interrupt_data?.question ?? "-";
  const context = item.interrupt_data?.context ?? "";
  const isPending = item.status === "pending";
  // For deployed runs, check if current user is the assignee.
  // For playground runs (no assigned_to), fall back to permission check only.
  const canAct = item.assigned_to ? isAssignee : true;

  const handleSubmit = () => {
    if (!selectedAction) return;
    onAction(item.thread_id, selectedAction, feedback);
    setFeedback("");
    setSelectedAction(null);
  };

  const handleCancel = () => {
    onCancel(item.thread_id);
    setFeedback("");
    setSelectedAction(null);
  };

  const handleDelegate = () => {
    if (!selectedDelegateUser) return;
    onDelegate(item.thread_id, selectedDelegateUser);
    setShowDelegateUI(false);
    setSelectedDelegateUser("");
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <IconComponent name="UserCheck" className="h-5 w-5 text-amber-500" />
            {t("Human Review Request")}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          {/* Agent + Status */}
          <div className="flex items-center justify-between">
            <div className="text-sm text-muted-foreground">
              <span className="font-medium text-foreground">
                {item.agent_name ?? item.agent_id.slice(0, 8) + "..."}
              </span>
              {" \u00b7 "}
              <span>{formatRelativeTime(item.requested_at, t)}</span>
            </div>
            <StatusBadge status={item.status} />
          </div>

          {/* Assigned To info + inline delegation */}
          {item.assigned_to_name && (
            <div className="rounded-md border border-border bg-muted/30 px-3 py-2 space-y-2">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-sm">
                  <IconComponent name="User" className="h-4 w-4 text-muted-foreground" />
                  <span className="text-muted-foreground">{t("Assigned to")}:</span>
                  <span className="font-medium text-foreground">{item.assigned_to_name}</span>
                  {item.delegated_by && item.delegated_at && (
                    <span className="text-xs text-muted-foreground">
                      ({t("delegated")} {formatRelativeTime(item.delegated_at, t)})
                    </span>
                  )}
                </div>
                {/* Inline delegate toggle */}
                {isPending && canAct && item.is_deployed_run && item.dept_id && !showDelegateUI && (
                  <button
                    onClick={() => setShowDelegateUI(true)}
                    disabled={isActing}
                    className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-primary hover:bg-primary/10 transition-colors disabled:opacity-50"
                  >
                    <IconComponent name="UserPlus" className="h-3.5 w-3.5" />
                    {t("Reassign")}
                  </button>
                )}
              </div>

              {/* Delegation dropdown — appears right below assigned to */}
              {isPending && canAct && showDelegateUI && (
                <div className="space-y-2 border-t border-border pt-2">
                  <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                    {t("Reassign to")}
                  </p>
                  {delegatableUsers.length === 0 ? (
                    <p className="text-sm text-muted-foreground">
                      {t("No users available in this department")}
                    </p>
                  ) : (
                    <select
                      value={selectedDelegateUser}
                      onChange={(e) => setSelectedDelegateUser(e.target.value)}
                      className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                    >
                      <option value="">{t("Select a user...")}</option>
                      {delegatableUsers.map((u) => (
                        <option key={u.id} value={u.id}>
                          {u.display_name}{u.email ? ` (${u.email})` : ""}
                        </option>
                      ))}
                    </select>
                  )}
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setShowDelegateUI(false);
                        setSelectedDelegateUser("");
                      }}
                    >
                      {t("Cancel")}
                    </Button>
                    <Button
                      size="sm"
                      onClick={handleDelegate}
                      disabled={!selectedDelegateUser || isActing}
                    >
                      {t("Confirm Reassign")}
                    </Button>
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Question */}
          <div>
            <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              {t("Question")}
            </p>
            <p className="rounded-md border border-border bg-muted/30 px-3 py-2 text-sm">
              {question}
            </p>
          </div>

          {/* Trigger Reason */}
          {item.interrupt_data?.auto_eval_reason && (
            <div>
              <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                {t("Trigger Reason")}
              </p>
              <div className="rounded-md border border-blue-200 bg-blue-50/50 px-3 py-2 dark:border-blue-800/50 dark:bg-blue-950/20">
                <p className="text-sm text-blue-800 dark:text-blue-300">
                  {item.interrupt_data.auto_eval_reason}
                </p>
                {item.interrupt_data.confidence != null && (
                  <div className="mt-2">
                    <p className="mb-1 text-xs text-blue-600 dark:text-blue-400">
                      {t("AI Confidence")}
                    </p>
                    <ConfidenceBar confidence={item.interrupt_data.confidence} />
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Context */}
          {context && (
            <div>
              <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                {t("Context")}
              </p>
              <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap rounded-md border border-border bg-muted/30 px-3 py-2 text-sm font-mono">
                {context}
              </pre>
            </div>
          )}

          {/* Decision (if already decided) */}
          {item.decision && !isPending && (
            <div>
              <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                {t("Decision")}
              </p>
              <div className="rounded-md border border-border bg-muted/30 px-3 py-2 text-sm space-y-1">
                <p>
                  <span className="font-medium">{t("Action")}:</span>{" "}
                  {item.decision.action}
                </p>
                {item.decision.feedback && (
                  <p>
                    <span className="font-medium">{t("Feedback")}:</span>{" "}
                    {item.decision.feedback}
                  </p>
                )}
              </div>
            </div>
          )}

          {/* Action buttons + feedback — only for pending */}
          {isPending && actions.length > 0 && (
            <>
              <div>
                <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  {t("Choose an action")}
                </p>
                <div className="flex flex-wrap gap-2">
                  {actions.map((action) => {
                    const isReject = action.toLowerCase().includes("reject");
                    const isSelected = selectedAction === action;
                    const canUseAction = canAct && (isReject ? canReject : canApprove);
                    return (
                      <button
                        key={action}
                        disabled={!canUseAction}
                        onClick={() =>
                          canUseAction && setSelectedAction(isSelected ? null : action)
                        }
                        title={!canUseAction ? t("You don't have permission") : action}
                        className={`rounded-md border px-4 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                          isSelected
                            ? isReject
                              ? "border-red-500 bg-red-500 text-white"
                              : "border-primary bg-primary text-primary-foreground"
                            : isReject
                              ? "border-red-300 text-red-600 hover:bg-red-50 dark:border-red-700 dark:text-red-400 dark:hover:bg-red-950/30"
                              : "border-border hover:bg-muted"
                        }`}
                      >
                        {action}
                      </button>
                    );
                  })}
                </div>
              </div>

              <div>
                <p className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  {t("Feedback (optional)")}
                </p>
                <textarea
                  value={feedback}
                  onChange={(e) => setFeedback(e.target.value)}
                  rows={3}
                  placeholder={t("Add a note for the agent...")}
                  className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                />
              </div>

              <div className="flex justify-end gap-2 pt-1">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleCancel}
                  disabled={isActing || !(canAct && canReject)}
                >
                  {t("Cancel Run")}
                </Button>
                <Button
                  size="sm"
                  onClick={handleSubmit}
                  disabled={!selectedAction || isActing || !canAct}
                >
                  {isActing ? t("Submitting...") : t("Submit Decision")}
                </Button>
              </div>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

export default function HITLApprovalsPage(): JSX.Element {
  const { t } = useTranslation();
  const { permissions, userData } = useContext(AuthContext);
  const setSuccessData = useAlertStore((state) => state.setSuccessData);
  const setErrorData = useAlertStore((state) => state.setErrorData);
  const can = (permissionKey: string) => permissions?.includes(permissionKey);
  const canApprove = can("hitl_approve");
  const canReject = can("hitl_reject");

  const [statusFilter, setStatusFilter] = useState<StatusFilter>("pending");
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedItem, setSelectedItem] = useState<HITLRequestItem | null>(
    null,
  );
  const [modalOpen, setModalOpen] = useState(false);
  const [actingThreadId, setActingThreadId] = useState<string | null>(null);
  const [optimisticStatusByThread, setOptimisticStatusByThread] = useState<
    Record<string, "approved" | "rejected" | "edited">
  >({});

  const queryStatus = statusFilter === "pending" ? "pending" : "all";
  const { data: allItems = [], isLoading, refetch } = useGetHitlPending(
    { status: queryStatus },
    { enabled: true },
  );

  const resumeMutation = useResumeHitl();
  const cancelMutation = useCancelHitl();
  const delegateMutation = useDelegateHitl();

  const itemsWithOptimistic = allItems.map((item) => {
    const optimisticStatus = optimisticStatusByThread[item.thread_id];
    if (optimisticStatus && item.status === "pending") {
      return { ...item, status: optimisticStatus };
    }
    return item;
  });

  // Client-side filter by status tab and search
  const filteredItems = itemsWithOptimistic.filter((item) => {
    const matchesStatus =
      statusFilter === "all" ||
      item.status === statusFilter;
    const matchesSearch =
      !searchQuery ||
      (item.interrupt_data?.question ?? "")
        .toLowerCase()
        .includes(searchQuery.toLowerCase()) ||
      (item.agent_name ?? "").toLowerCase().includes(searchQuery.toLowerCase());
    return matchesStatus && matchesSearch;
  });

  const pendingCount = itemsWithOptimistic.filter((i) => i.status === "pending").length;

  // Clear optimistic entries once backend status catches up.
  useEffect(() => {
    setOptimisticStatusByThread((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const item of allItems) {
        if (next[item.thread_id] && item.status !== "pending") {
          delete next[item.thread_id];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [allItems]);

  const handleAction = (
    threadId: string,
    action: string,
    feedback: string,
  ) => {
    const actionLower = action.toLowerCase();
    const optimisticStatus: "approved" | "rejected" | "edited" =
      actionLower.includes("reject")
        ? "rejected"
        : actionLower.includes("edit")
          ? "edited"
          : "approved";

    // Optimistically reflect resolution immediately in HITL Approvals UI.
    setOptimisticStatusByThread((prev) => ({
      ...prev,
      [threadId]: optimisticStatus,
    }));
    setModalOpen(false);
    setSelectedItem(null);
    setSuccessData({ title: t("Decision submitted successfully") });

    resumeMutation.mutate(
      { thread_id: threadId, action, feedback },
      {
        onSuccess: () => {},
        onError: (err: any) => {
          // Roll back optimistic state when resume fails.
          setOptimisticStatusByThread((prev) => {
            const next = { ...prev };
            delete next[threadId];
            return next;
          });
          setErrorData({
            title: t("Failed to submit decision"),
            list: [err?.response?.data?.detail ?? String(err)],
          });
        },
        onSettled: () => {
          // Trigger an immediate refresh; backend may still show pending until
          // resume finishes, optimistic status keeps UI responsive meanwhile.
          refetch();
        },
      },
    );
  };

  const handleCancel = (threadId: string) => {
    setActingThreadId(threadId);
    cancelMutation.mutate(
      { thread_id: threadId },
      {
        onSuccess: () => {
          setSuccessData({ title: t("Run cancelled") });
          setModalOpen(false);
          setSelectedItem(null);
        },
        onError: (err: any) => {
          setErrorData({
            title: t("Failed to cancel run"),
            list: [err?.response?.data?.detail ?? String(err)],
          });
        },
        onSettled: () => setActingThreadId(null),
      },
    );
  };

  const handleDelegate = (threadId: string, userId: string) => {
    setActingThreadId(threadId);
    delegateMutation.mutate(
      { thread_id: threadId, delegate_to_user_id: userId },
      {
        onSuccess: () => {
          setSuccessData({ title: t("Request delegated successfully") });
          setModalOpen(false);
          setSelectedItem(null);
        },
        onError: (err: any) => {
          setErrorData({
            title: t("Failed to delegate request"),
            list: [err?.response?.data?.detail ?? String(err)],
          });
        },
        onSettled: () => setActingThreadId(null),
      },
    );
  };

  const openDetail = (item: HITLRequestItem) => {
    setSelectedItem(item);
    setModalOpen(true);
  };

  const TABS: { label: string; value: StatusFilter }[] = [
    { label: t("All"), value: "all" },
    { label: t("Pending"), value: "pending" },
    { label: t("Approved"), value: "approved" },
    { label: t("Rejected"), value: "rejected" },
    { label: t("Cancelled"), value: "cancelled" },
  ];

  return (
    <div className="flex h-full w-full flex-col overflow-hidden bg-background">
      {/* -- Header -- */}
      <div className="border-b border-border px-6 py-3">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-lg font-semibold text-foreground">
                  {t("HITL Approvals")}
                </h1>
                {pendingCount > 0 && (
                  <span className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-400">
                    {t("{{count}} pending", { count: pendingCount })}
                  </span>
                )}
              </div>
              <p className="text-sm text-muted-foreground">
                {t("Paused agent runs awaiting human review")}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            {/* Search */}
            <div className="relative">
              <IconComponent
                name="Search"
                className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
              />
              <input
                type="text"
                placeholder={t("Search by question or agent name...")}
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-64 rounded-md border border-border bg-background py-2 pl-9 pr-4 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => refetch()}
              className="gap-1.5"
            >
              <IconComponent name="RefreshCw" className="h-3.5 w-3.5" />
              {t("Refresh")}
            </Button>
          </div>
        </div>

        {/* Status tabs */}
        <div className="mt-3 flex gap-1 border-b border-transparent">
          {TABS.map((tab) => (
            <button
              key={tab.value}
              onClick={() => setStatusFilter(tab.value)}
              className={`relative px-3 py-1.5 text-sm font-medium transition-colors ${
                statusFilter === tab.value
                  ? "text-foreground after:absolute after:bottom-0 after:left-0 after:right-0 after:h-0.5 after:bg-primary"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {tab.label}
              {tab.value === "pending" && pendingCount > 0 && (
                <span className="ml-1.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-500 px-1 text-xxs font-bold text-white">
                  {pendingCount}
                </span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* -- Table -- */}
      <div className="flex-1 overflow-auto px-6 py-4">
        {isLoading ? (
          <div className="flex h-40 items-center justify-center">
            <IconComponent
              name="Loader2"
              className="h-6 w-6 animate-spin text-muted-foreground"
            />
          </div>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-border bg-card">
            <table className="w-full">
              <thead className="bg-muted/50">
                <tr className="border-b border-border">
                  {[
                    t("Agent"),
                    t("Question"),
                    t("Reason"),
                    t("Actions"),
                    t("Assigned To"),
                    t("Requested"),
                    t("Status"),
                    "",
                  ].map((h, idx) => (
                    <th
                      key={h || `col-${idx}`}
                      className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-muted-foreground"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {filteredItems.length === 0 ? (
                  <tr>
                    <td
                      colSpan={8}
                      className="px-4 py-12 text-center text-sm text-muted-foreground"
                    >
                      <IconComponent
                        name="UserCheck"
                        className="mx-auto mb-2 h-8 w-8 opacity-30"
                      />
                      {statusFilter === "pending"
                        ? t("No pending approvals \u2014 all clear!")
                        : t("No items found")}
                    </td>
                  </tr>
                ) : (
                  filteredItems.map((item) => {
                    const actions = item.interrupt_data?.actions ?? [];
                    const question = item.interrupt_data?.question ?? "-";
                    const isPending = item.status === "pending";
                    const isActing = actingThreadId === item.thread_id;

                    return (
                      <tr
                        key={item.id}
                        className="group cursor-pointer hover:bg-muted/40"
                        onClick={() => openDetail(item)}
                      >
                        {/* Agent */}
                        <td className="px-4 py-3">
                          <p className="text-sm font-medium text-foreground">
                            {item.agent_name ?? "-"}
                          </p>
                          <p className="text-xs text-muted-foreground">
                            {item.agent_id.slice(0, 8)}...
                          </p>
                        </td>

                        {/* Question */}
                        <td className="max-w-xs px-4 py-3">
                          <p
                            className="truncate text-sm text-foreground"
                            title={question}
                          >
                            {question}
                          </p>
                        </td>

                        {/* Reason */}
                        <td className="max-w-[200px] px-4 py-3">
                          {item.interrupt_data?.auto_eval_reason ? (
                            <div className="flex items-center">
                              <p
                                className="truncate text-xs text-muted-foreground"
                                title={item.interrupt_data.auto_eval_reason}
                              >
                                {item.interrupt_data.auto_eval_reason}
                              </p>
                              {item.interrupt_data.confidence != null && (
                                <ConfidenceBadge confidence={item.interrupt_data.confidence} />
                              )}
                            </div>
                          ) : (
                            <span className="text-xs text-muted-foreground/50">-</span>
                          )}
                        </td>

                        {/* Actions badges */}
                        <td className="px-4 py-3">
                          <div className="flex flex-wrap gap-1">
                            {actions.map((a) => (
                              <span
                                key={a}
                                className="rounded border border-border bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
                              >
                                {a}
                              </span>
                            ))}
                          </div>
                        </td>

                        {/* Assigned To */}
                        <td className="px-4 py-3">
                          {item.assigned_to_name ? (
                            <p
                              className="max-w-[180px] truncate text-sm text-foreground"
                              title={item.assigned_to_name}
                            >
                              {item.assigned_to_name}
                            </p>
                          ) : (
                            <span className="text-xs text-muted-foreground/50">-</span>
                          )}
                        </td>

                        {/* Time */}
                        <td className="px-4 py-3 text-sm text-muted-foreground whitespace-nowrap">
                          {formatRelativeTime(item.requested_at, t)}
                        </td>

                        {/* Status */}
                        <td className="px-4 py-3">
                          <StatusBadge status={item.status} />
                        </td>

                        {/* Action buttons (only for pending) */}
                        <td
                          className="px-4 py-3"
                          onClick={(e) => e.stopPropagation()}
                        >
                          {isPending && (
                            <div className="flex gap-1.5 opacity-0 transition-opacity group-hover:opacity-100">
                              {actions
                                .slice(0, 2)
                                .map((action) => {
                                  const isReject = action
                                    .toLowerCase()
                                    .includes("reject");
                                  return (
                                    <button
                                      key={action}
                                      disabled={isActing || (isReject ? !canReject : !canApprove)}
                                      onClick={() =>
                                        (isReject ? canReject : canApprove) &&
                                        handleAction(item.thread_id, action, "")
                                      }
                                      title={action}
                                      className={`rounded px-2.5 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                                        isReject
                                          ? "bg-red-100 text-red-700 hover:bg-red-200 dark:bg-red-900/30 dark:text-red-400 dark:hover:bg-red-900/50"
                                          : "bg-green-100 text-green-700 hover:bg-green-200 dark:bg-green-900/30 dark:text-green-400 dark:hover:bg-green-900/50"
                                      }`}
                                    >
                                      {isActing ? "\u2026" : action}
                                    </button>
                                  );
                                })}
                              <button
                                disabled={isActing || !canReject}
                                onClick={() => handleCancel(item.thread_id)}
                                title={t("Cancel run")}
                                className="rounded px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
                              >
                                \u2715
                              </button>
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* -- Detail Modal -- */}
      <DetailModal
        item={selectedItem}
        open={modalOpen}
        onClose={() => {
          setModalOpen(false);
          setSelectedItem(null);
        }}
        onAction={handleAction}
        onCancel={handleCancel}
        onDelegate={handleDelegate}
        isActing={actingThreadId === selectedItem?.thread_id}
        canApprove={canApprove}
        canReject={canReject}
        isAssignee={
          !!userData?.id && selectedItem?.assigned_to === userData.id
        }
        currentUserId={userData?.id}
      />
    </div>
  );
}
