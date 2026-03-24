import { memo, useMemo, useRef, useState } from "react";
import { useHotkeys } from "react-hotkeys-hook";
import { useSearchParams } from "react-router-dom";
import { useShallow } from "zustand/react/shallow";
import IconComponent from "@/components/common/genericIconComponent";
import ShadTooltip from "@/components/common/shadTooltipComponent";
import AgentSettingsComponent from "@/components/core/agentSettingsComponent";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { SAVED_HOVER } from "@/constants/constants";
import { useGetRefreshAgentsQuery } from "@/controllers/API/queries/agents/use-get-refresh-agents-query";
import { useGetFoldersQuery } from "@/controllers/API/queries/folders/use-get-folders";
import { useCustomNavigate } from "@/customization/hooks/use-custom-navigate";
import useSaveAgent from "@/hooks/agents/use-save-agent";
import { useUnsavedChanges } from "@/hooks/use-unsaved-changes";
import useAlertStore from "@/stores/alertStore";
import useAgentStore from "@/stores/agentStore";
import useAgentsManagerStore from "@/stores/agentsManagerStore";
import { useShortcutsStore } from "@/stores/shortcuts";
import { swatchColors } from "@/utils/styleUtils";
import { cn, getNumberFromString } from "@/utils/utils";

export const MenuBar = memo((): JSX.Element => {
  const setSuccessData = useAlertStore((state) => state.setSuccessData);
  const saveLoading = useAgentsManagerStore((state) => state.saveLoading);
  const [openSettings, setOpenSettings] = useState(false);
  const navigate = useCustomNavigate();
  const isBuilding = useAgentStore((state) => state.isBuilding);
  const saveAgent = useSaveAgent();
  const autoSaving = useAgentsManagerStore((state) => state.autoSaving);
  const {
    isAgentLocked,
    currentAgentName,
    currentAgentId,
    currentAgentFolderId,
    currentAgentIcon,
    currentAgentGradient,
  } = useAgentStore(
    useShallow((state) => ({
      isAgentLocked: state.currentAgent?.locked,
      currentAgentName: state.currentAgent?.name,
      currentAgentId: state.currentAgent?.id,
      currentAgentFolderId: state.currentAgent?.project_id,
      currentAgentIcon: state.currentAgent?.icon,
      currentAgentGradient: state.currentAgent?.gradient,
    })),
  );
  const { updated_at: updatedAt } = useAgentsManagerStore(
    useShallow((state) => ({
      updated_at: state.currentAgent?.updated_at,
    })),
  );
  const onAgentBuilderPage = useAgentStore((state) => state.onAgentBuilderPage);
  const measureRef = useRef<HTMLSpanElement>(null);
  const changesNotSaved = useUnsavedChanges();
  const [searchParams] = useSearchParams();
  const isReadOnlyMode = searchParams.get("readonly") === "1";

  const { data: folders, isFetched: isFoldersFetched } = useGetFoldersQuery();

  useGetRefreshAgentsQuery(
    {
      get_all: true,
      header_agents: true,
    },
    { enabled: isFoldersFetched },
  );

  const currentFolder = useMemo(
    () => folders?.find((f) => f.id === currentAgentFolderId),
    [folders, currentAgentFolderId],
  );

  const handleSave = () => {
    if (isReadOnlyMode) return;
    saveAgent().then(() => {
      setSuccessData({ title: "Saved successfully" });
    });
  };

  const changes = useShortcutsStore((state) => state.changesSave);
  useHotkeys(changes, handleSave, { preventDefault: true });

  const swatchIndex =
    (currentAgentGradient && !isNaN(parseInt(currentAgentGradient))
      ? parseInt(currentAgentGradient)
      : getNumberFromString(currentAgentGradient ?? currentAgentId ?? "")) %
    swatchColors.length;

  return onAgentBuilderPage ? (
    <Popover
      open={isReadOnlyMode ? false : openSettings}
      onOpenChange={isReadOnlyMode ? undefined : setOpenSettings}
    >
      <PopoverAnchor>
        <div
          className="relative flex w-full items-center justify-center gap-2"
          data-testid="menu_bar_wrapper"
        >
          <div
            className="header-menu-bar hidden max-w-40 justify-end truncate md:flex xl:max-w-full"
            data-testid="menu_agent_bar"
            id="menu_agent_bar_navigation"
          >
            {currentFolder?.name && (
              <div className="hidden truncate md:flex">
                <div
                  className="cursor-pointer truncate text-sm text-muted-foreground hover:text-primary"
                  onClick={() => {
                    navigate(
                      currentFolder?.id
                        ? "/all/folder/" + currentFolder.id
                        : "/all",
                    );
                  }}
                >
                  {currentFolder?.name}
                </div>
              </div>
            )}
          </div>
          <div
            className="hidden w-fit shrink-0 select-none font-normal text-muted-foreground md:flex"
            data-testid="menu_bar_separator"
          >
            /
          </div>
          <div className="flex items-center justify-center rounded-lg bg-muted p-1.5">
  <IconComponent
    name="Workagent"
    className="h-3.5 w-3.5"
  />
</div>
          {isReadOnlyMode ? (
            <div
              className="relative -mr-5 flex shrink-0 items-center gap-2 text-sm sm:whitespace-normal"
              data-testid="menu_bar_display"
            >
              <span
                ref={measureRef}
                className="w-fit max-w-[35vw] truncate whitespace-pre text-mmd font-semibold sm:max-w-full sm:text-sm"
                aria-hidden="true"
                data-testid="agent_name"
              >
                {currentAgentName || "Untitled agent"}
              </span>
            </div>
          ) : (
            <PopoverTrigger asChild>
              <div
                className="group relative -mr-5 flex shrink-0 cursor-pointer items-center gap-2 text-sm sm:whitespace-normal"
                data-testid="menu_bar_display"
              >
                <span
                  ref={measureRef}
                  className="w-fit max-w-[35vw] truncate whitespace-pre text-mmd font-semibold sm:max-w-full sm:text-sm"
                  aria-hidden="true"
                  data-testid="agent_name"
                >
                  {currentAgentName || "Untitled agent"}
                </span>
                <IconComponent
                  name="pencil"
                  className={cn(
                    "h-5 w-3.5 -translate-x-2 opacity-0 transition-all",
                    !openSettings &&
                      "sm:group-hover:translate-x-0 sm:group-hover:opacity-100",
                  )}
                />
              </div>
            </PopoverTrigger>
          )}
          <div className={"ml-5 hidden shrink-0 items-center sm:flex"}>
            {!autoSaving && !isReadOnlyMode && (
              <ShadTooltip
                content={
                  changesNotSaved
                    ? saveLoading
                      ? "Saving..."
                      : "Save Changes"
                    : SAVED_HOVER +
                      (updatedAt
                        ? new Date(updatedAt).toLocaleString("en-US", {
                            hour: "numeric",
                            minute: "numeric",
                          })
                        : "Never")
                }
                side="bottom"
                styleClasses="cursor-default z-10"
              >
                <div>
                  <Button
                    variant="primary"
                    size="iconMd"
                    disabled={!changesNotSaved || isBuilding || saveLoading}
                    className={cn("h-7 w-7 border-border")}
                    onClick={handleSave}
                    data-testid="save-agent-button"
                  >
                    <IconComponent
                      name={saveLoading ? "Loader2" : "Save"}
                      className={cn("h-5 w-5", saveLoading && "animate-spin")}
                    />
                  </Button>
                </div>
              </ShadTooltip>
            )}
          </div>
        </div>
      </PopoverAnchor>
      <PopoverContent
        className="flex w-96 flex-col gap-4 p-4"
        align="center"
        sideOffset={15}
      >
        {!isReadOnlyMode && (
          <AgentSettingsComponent
            close={() => setOpenSettings(false)}
            open={openSettings}
          />
        )}
      </PopoverContent>
    </Popover>
  ) : (
    <></>
  );
});

export default MenuBar;
