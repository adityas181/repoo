from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_serializer,
)

from agentcore.graph_langgraph import RunOutputs
from agentcore.schema.dotdict import dotdict
from agentcore.schema.graph import Tweaks
from agentcore.schema.schema import InputType, OutputType, OutputValue
from agentcore.serialization.serialization import get_max_items_length, get_max_text_length, serialize
from agentcore.services.database.models.base import orjson_dumps
from agentcore.services.database.models.agent.model import AgentCreate, AgentRead
from agentcore.services.database.models.user.model import UserRead
from agentcore.services.settings.base import Settings
from agentcore.services.settings.feature_flags import FEATURE_FLAGS, FeatureFlags
from agentcore.services.tracing.schema import Log


class BuildStatus(Enum):
    """Status of the build."""

    SUCCESS = "success"
    FAILURE = "failure"
    STARTED = "started"
    IN_PROGRESS = "in_progress"


class TweaksRequest(BaseModel):
    tweaks: dict[str, dict[str, Any]] | None = Field(default_factory=dict)


class UpdateTemplateRequest(BaseModel):
    template: dict


class TaskResponse(BaseModel):
    """Task response schema."""

    id: str | None = Field(None)
    href: str | None = Field(None)


class ProcessResponse(BaseModel):
    """Process response schema."""

    result: Any
    status: str | None = None
    task: TaskResponse | None = None
    session_id: str | None = None
    backend: str | None = None


class RunResponse(BaseModel):
    """Run response schema."""

    outputs: list[RunOutputs] | None = []
    session_id: str | None = None

    @model_serializer(mode="plain")
    def serialize(self):
        # Serialize all the outputs if they are base models
        serialized = {"session_id": self.session_id, "outputs": []}
        if self.outputs:
            serialized_outputs = []
            for output in self.outputs:
                if isinstance(output, BaseModel) and not isinstance(output, RunOutputs):
                    serialized_outputs.append(output.model_dump(exclude_none=True))
                else:
                    serialized_outputs.append(output)
            serialized["outputs"] = serialized_outputs
        return serialized


class PreloadResponse(BaseModel):
    """Preload response schema."""

    session_id: str | None = None
    is_clear: bool | None = None


class TaskStatusResponse(BaseModel):
    """Task status response schema."""

    status: str
    result: Any | None = None


class ChatMessage(BaseModel):
    """Chat message schema."""

    is_bot: bool = False
    message: str | None | dict = None
    chat_key: str | None = Field(None, serialization_alias="chatKey")
    type: str = "human"


class ChatResponse(ChatMessage):
    """Chat response schema."""

    intermediate_steps: str

    type: str
    is_bot: bool = True
    files: list = []

    @field_validator("type")
    @classmethod
    def validate_message_type(cls, v):
        if v not in {"start", "stream", "end", "error", "info", "file"}:
            msg = "type must be start, stream, end, error, info, or file"
            raise ValueError(msg)
        return v


class PromptResponse(ChatMessage):
    """Prompt response schema."""

    prompt: str
    type: str = "prompt"
    is_bot: bool = True


class FileResponse(ChatMessage):
    """File response schema."""

    data: Any = None
    data_type: str
    type: str = "file"
    is_bot: bool = True

    @field_validator("data_type")
    @classmethod
    def validate_data_type(cls, v):
        if v not in {"image", "csv"}:
            msg = "data_type must be image or csv"
            raise ValueError(msg)
        return v


class AgentListCreate(BaseModel):
    agents: list[AgentCreate]


class AgentListIds(BaseModel):
    agent_ids: list[str]


class AgentListRead(BaseModel):
    agents: list[AgentRead]


class AgentListReadWithFolderName(BaseModel):
    agents: list[AgentRead]
    folder_name: str
    description: str


class InitResponse(BaseModel):
    agent_id: str = Field(serialization_alias="agentId")


class BuiltResponse(BaseModel):
    built: bool


class UploadFileResponse(BaseModel):
    """Upload file response schema."""

    agent_id: str = Field(serialization_alias="agentId")
    file_path: Path


class StreamData(BaseModel):
    event: str
    data: dict

    def __str__(self) -> str:
        return f"event: {self.event}\ndata: {orjson_dumps(self.data, indent_2=False)}\n\n"


class CustomComponentRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    code: str
    frontend_node: dict | None = None


class CustomComponentResponse(BaseModel):
    data: dict
    type: str


class UpdateCustomComponentRequest(CustomComponentRequest):
    field: str
    field_value: str | int | float | bool | dict | list | None = None
    template: dict
    tool_mode: bool = False

    def get_template(self):
        return dotdict(self.template)


class CustomComponentResponseError(BaseModel):
    detail: str
    traceback: str


class ComponentListCreate(BaseModel):
    agents: list[AgentCreate]


class ComponentListRead(BaseModel):
    agents: list[AgentRead]


class UsersResponse(BaseModel):
    total_count: int
    users: list[UserRead]


class ApiKeyResponse(BaseModel):
    id: str
    api_key: str
    name: str
    created_at: str
    last_used_at: str


class ApiKeysResponse(BaseModel):
    """API Keys response - returns empty list ( Azure Key Vault)."""
    total_count: int
    user_id: UUID
    api_keys: list = []  


class CreateApiKeyRequest(BaseModel):
    name: str


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str


class ApiKeyCreateRequest(BaseModel):
    api_key: str


class AgentApiKeyResponse(BaseModel):
    """Agent API key info for list views (never includes full key)."""
    id: UUID
    agent_id: UUID
    deployment_id: UUID
    version: str
    environment: str
    key_prefix: str
    is_active: bool
    created_at: str
    last_used_at: str | None = None
    expires_at: str | None = None


class AgentApiKeyCreatedResponse(AgentApiKeyResponse):
    """Returned only at creation/rotation time — includes the plaintext key."""
    api_key: str


class VerticesOrderResponse(BaseModel):
    ids: list[str]
    run_id: UUID
    vertices_to_run: list[str]


class ResultDataResponse(BaseModel):
    results: Any | None = Field(default_factory=dict)
    outputs: dict[str, OutputValue] = Field(default_factory=dict)
    logs: dict[str, list[Log]] = Field(default_factory=dict)
    message: Any | None = Field(default_factory=dict)
    artifacts: Any | None = Field(default_factory=dict)
    timedelta: float | None = None
    duration: str | None = None
    used_frozen_result: bool | None = False

    @field_serializer("results")
    @classmethod
    def serialize_results(cls, v):
        """Serializes the results value with custom handling for special types and applies truncation limits.

        Returns:
            The serialized representation of the input value, truncated according to configured
            maximum text length and item count.
        """
        return serialize(v, max_length=get_max_text_length(), max_items=get_max_items_length())

    @model_serializer(mode="plain")
    def serialize_model(self) -> dict:
        """Serialize the entire model into a dictionary with truncation applied to large fields.

        Returns:
            dict: A dictionary representation of the model with serialized and truncated
            results, outputs, logs, message, and artifacts.
        """
        return {
            "results": self.serialize_results(self.results),
            "outputs": serialize(self.outputs, max_length=get_max_text_length(), max_items=get_max_items_length()),
            "logs": serialize(self.logs, max_length=get_max_text_length(), max_items=get_max_items_length()),
            "message": serialize(self.message, max_length=get_max_text_length(), max_items=get_max_items_length()),
            "artifacts": serialize(self.artifacts, max_length=get_max_text_length(), max_items=get_max_items_length()),
            "timedelta": self.timedelta,
            "duration": self.duration,
            "used_frozen_result": self.used_frozen_result,
        }


class VertexBuildResponse(BaseModel):
    id: str | None = None
    inactivated_vertices: list[str] | None = None
    next_vertices_ids: list[str] | None = None
    top_level_vertices: list[str] | None = None
    valid: bool
    params: Any | None = Field(default_factory=dict)
    """JSON string of the params."""
    data: ResultDataResponse
    """Mapping of vertex ids to result dict containing the param name and result value."""
    timestamp: datetime | None = Field(default_factory=lambda: datetime.now(timezone.utc))
    """Timestamp of the build."""

    @field_serializer("data")
    def serialize_data(self, data: ResultDataResponse) -> dict:
        """Serialize a ResultDataResponse object into a dictionary with enforced maximum text and item lengths.

        Parameters:
            data (ResultDataResponse): The data object to serialize.

        Returns:
            dict: The serialized representation of the data with truncation applied.
        """
        return serialize(data, max_length=get_max_text_length(), max_items=get_max_items_length())


class VerticesBuiltResponse(BaseModel):
    vertices: list[VertexBuildResponse]


class InputValueRequest(BaseModel):
    components: list[str] | None = []
    input_value: str | None = None
    session: str | None = None
    type: InputType | None = Field(
        "any",
        description="Defines on which components the input value should be applied. "
        "'any' applies to all input components.",
    )

    # add an example
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "components": ["components_id", "Component Name"],
                    "input_value": "input_value",
                    "session": "session_id",
                },
                {"components": ["Component Name"], "input_value": "input_value"},
                {"input_value": "input_value"},
                {
                    "components": ["Component Name"],
                    "input_value": "input_value",
                    "session": "session_id",
                },
                {"input_value": "input_value", "session": "session_id"},
                {"type": "chat", "input_value": "input_value"},
                {"type": "json", "input_value": '{"key": "value"}'},
            ]
        },
        extra="forbid",
    )


class SimplifiedAPIRequest(BaseModel):
    input_value: str | None = Field(default=None, description="The input value")
    input_type: InputType | None = Field(default="chat", description="The input type")
    output_type: OutputType | None = Field(default="chat", description="The output type")
    output_component: str | None = Field(
        default="",
        description="If there are multiple output components, you can specify the component to get the output from.",
    )
    tweaks: Tweaks | None = Field(default=None, description="The tweaks")
    session_id: str | None = Field(default=None, description="The session id")
    files: list[str] | None = Field(default=None, description="List of file paths for uploaded files")
    # Internal-only HITL resume payload (used by /api/run resume mode).
    hitl_action: str | None = Field(default=None, description="HITL action name for resume mode")
    hitl_feedback: str | None = Field(default=None, description="Optional HITL feedback for resume mode")
    hitl_edited_value: str | None = Field(default=None, description="Optional edited value for resume mode")


# (alias) type ReactFlowJsonObject<NodeData = any, EdgeData = any> = {
#     nodes: Node<NodeData>[];
#     edges: Edge<EdgeData>[];
#     viewport: Viewport;
# }
# import ReactFlowJsonObject
class AgentDataRequest(BaseModel):
    nodes: list[dict]
    edges: list[dict]
    viewport: dict | None = None


class ConfigResponse(BaseModel):
    feature_flags: FeatureFlags
    serialization_max_items_length: int
    serialization_max_text_length: int
    frontend_timeout: int
    auto_saving: bool
    auto_saving_interval: int
    health_check_max_retries: int
    max_file_size_upload: int
    public_agent_cleanup_interval: int
    public_agent_expiration: int
    event_delivery: Literal["polling", "streaming", "direct"]

    @classmethod
    def from_settings(cls, settings: Settings) -> "ConfigResponse":
        """Create a ConfigResponse instance using values from a Settings object and global feature flags.

        Parameters:
            settings (Settings): The Settings object containing configuration values.

        Returns:
            ConfigResponse: An instance populated with configuration and feature flag values.
        """
        return cls(
            feature_flags=FEATURE_FLAGS,
            serialization_max_items_length=settings.max_items_length,
            serialization_max_text_length=settings.max_text_length,
            frontend_timeout=settings.frontend_timeout,
            auto_saving=settings.auto_saving,
            auto_saving_interval=settings.auto_saving_interval,
            health_check_max_retries=settings.health_check_max_retries,
            max_file_size_upload=settings.max_file_size_upload,
            public_agent_cleanup_interval=settings.public_agent_cleanup_interval,
            public_agent_expiration=settings.public_agent_expiration,
            event_delivery=settings.event_delivery,
        )


class CancelAgentResponse(BaseModel):
    """Response model for agent build cancellation."""

    success: bool
    message: str

