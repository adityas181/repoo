import { Copy, Check, Key, RotateCw } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import {
  oneDark,
  oneLight,
} from "react-syntax-highlighter/dist/cjs/styles/prism";
import IconComponent from "@/components/common/genericIconComponent";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useDarkStore } from "@/stores/darkStore";
import { api } from "@/controllers/API/api";
import { customGetHostProtocol } from "@/customization/utils/custom-get-host-protocol";

interface ExportApiModalProps {
  open: boolean;
  setOpen: (open: boolean) => void;
  agentId: string;
  agentName: string;
  version: string;
  environment: "uat" | "prod";
  deployId: string;
}

type TabType = "cURL" | "Python" | "JavaScript";

export default function ExportApiModal({
  open,
  setOpen,
  agentId,
  agentName,
  version,
  environment,
  deployId,
}: ExportApiModalProps) {
  const { t } = useTranslation();
  const dark = useDarkStore((state) => state.dark);
  const [selectedTab, setSelectedTab] = useState<TabType>("cURL");
  const [apiKey, setApiKey] = useState<string | null>(null);
  const [apiKeyPrefix, setApiKeyPrefix] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [copiedField, setCopiedField] = useState<string | null>(null);

  const { protocol, host } = customGetHostProtocol();
  const baseUrl = `${protocol}//${host}`;
  const envCode = environment === "uat" ? 1 : 2;
  const runUrl = `${baseUrl}/api/run/${agentId}?env=${envCode}&version=${version}`;

  useEffect(() => {
    if (!open) {
      setApiKey(null);
      setApiKeyPrefix(null);
    }
  }, [open]);

  const handleRotateKey = async () => {
    setLoading(true);
    try {
      const response = await api.post(
        `/api/api_key/agent/${agentId}/rotate?environment=${environment}&version=${version}`,
      );
      setApiKey(response.data.api_key);
      setApiKeyPrefix(response.data.key_prefix);
    } catch (error) {
      console.error("Failed to rotate API key:", error);
    } finally {
      setLoading(false);
    }
  };

  const copyToClipboard = (text: string, field: string) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopiedField(field);
      setTimeout(() => setCopiedField(null), 2000);
    });
  };

  const displayKey = apiKey || "<YOUR_API_KEY>";

  const curlCode = `curl --request POST \\
  '${runUrl}' \\
  --header 'Content-Type: application/json' \\
  --header 'x-api-key: ${displayKey}' \\
  --data '{
    "input_value": "Hello!",
    "session_id": "YOUR_SESSION_ID_HERE"
  }'`;

  const pythonCode = `import requests
import uuid

url = "${runUrl}"
headers = {
    "Content-Type": "application/json",
    "x-api-key": "${displayKey}"
}
# Use one session_id per conversation and reuse it for follow-up calls.
session_id = str(uuid.uuid4())
payload = {
    "input_value": "Hello!",
    "session_id": session_id
}

response = requests.post(url, json=payload, headers=headers)
print("session_id:", response.headers.get("X-Session-Id", session_id))
print(response.json())`;

  const jsCode = `// Use one session_id per conversation and reuse it for follow-up calls.
const sessionId = crypto.randomUUID();

const response = await fetch("${runUrl}", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "x-api-key": "${displayKey}"
  },
  body: JSON.stringify({
    input_value: "Hello!",
    session_id: sessionId
  })
});

const data = await response.json();
console.log("session_id:", response.headers.get("X-Session-Id") || sessionId);
console.log(data);`;

  const tabs: { title: TabType; icon: string; language: string; code: string }[] = [
    { title: "cURL", icon: "TerminalSquare", language: "bash", code: curlCode },
    { title: "Python", icon: "BWPython", language: "python", code: pythonCode },
    { title: "JavaScript", icon: "javascript", language: "javascript", code: jsCode },
  ];

  const currentTab = tabs.find((tab) => tab.title === selectedTab);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <IconComponent name="Code2" className="h-5 w-5" />
            {t("API Access")} — {agentName} ({version}, {environment.toUpperCase()})
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          {/* API Key Section */}
          <div className="rounded-lg border bg-muted/30 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-sm font-medium">
                <Key className="h-4 w-4" />
                {t("API Key")}
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={handleRotateKey}
                disabled={loading}
                className="gap-1.5"
              >
                <RotateCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
                {apiKey ? t("Rotate Key") : t("Generate Key")}
              </Button>
            </div>

            {apiKey ? (
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <code className="flex-1 rounded bg-background px-3 py-2 text-xs font-mono break-all border">
                    {apiKey}
                  </code>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="shrink-0"
                    onClick={() => copyToClipboard(apiKey, "apikey")}
                  >
                    {copiedField === "apikey" ? (
                      <Check className="h-4 w-4 text-green-500" />
                    ) : (
                      <Copy className="h-4 w-4" />
                    )}
                  </Button>
                </div>
                <p className="text-xs text-amber-600 dark:text-amber-400">
                  {t("Save this key now — it won't be shown again after you close this dialog.")}
                </p>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">
                {t("Click \"Generate Key\" to create an API key for this deployment. If a key already exists, it will be rotated.")}
              </p>
            )}
          </div>

          {/* Endpoint URLs */}
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-muted-foreground w-16 shrink-0">{t("Run")}</span>
              <code className="flex-1 rounded bg-muted px-2 py-1 text-xs font-mono truncate">
                {runUrl}
              </code>
              <Button
                variant="ghost"
                size="icon"
                className="shrink-0 h-7 w-7"
                onClick={() => copyToClipboard(runUrl, "runurl")}
              >
                {copiedField === "runurl" ? (
                  <Check className="h-3.5 w-3.5 text-green-500" />
                ) : (
                  <Copy className="h-3.5 w-3.5" />
                )}
              </Button>
            </div>
          </div>

          {/* Code Tabs */}
          <div className="rounded-lg border overflow-hidden">
            {/* Tab headers */}
            <div className="flex border-b bg-muted/30">
              {tabs.map((tab) => (
                <button
                  key={tab.title}
                  type="button"
                  className={`flex items-center gap-1.5 px-4 py-2 text-xs font-medium border-b-2 transition-colors ${
                    selectedTab === tab.title
                      ? "border-foreground text-foreground"
                      : "border-transparent text-muted-foreground hover:text-foreground"
                  }`}
                  onClick={() => setSelectedTab(tab.title)}
                >
                  <IconComponent name={tab.icon} className="h-3.5 w-3.5" />
                  {tab.title}
                </button>
              ))}

              {/* Copy button */}
              <div className="ml-auto flex items-center pr-2">
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={() =>
                    currentTab && copyToClipboard(currentTab.code, "code")
                  }
                >
                  {copiedField === "code" ? (
                    <Check className="h-3.5 w-3.5 text-green-500" />
                  ) : (
                    <Copy className="h-3.5 w-3.5" />
                  )}
                </Button>
              </div>
            </div>

            {/* Code content */}
            <div className="max-h-64 overflow-auto">
              {currentTab && (
                <SyntaxHighlighter
                  language={currentTab.language}
                  style={dark ? oneDark : oneLight}
                  customStyle={{
                    margin: 0,
                    borderRadius: 0,
                    fontSize: "12px",
                  }}
                  wrapLongLines
                >
                  {currentTab.code}
                </SyntaxHighlighter>
              )}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
