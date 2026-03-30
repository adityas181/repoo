from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=True)

import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any, Literal

import orjson
import yaml
from aiofile import async_open
from loguru import logger
from pydantic import Field, field_validator

from agentcore.services.ltm import LTM_DEFAULTS as _LTM_D
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from typing_extensions import override

from agentcore.serialization.constants import MAX_ITEMS_LENGTH, MAX_TEXT_LENGTH
# [VARIABLE REMOVED] from agentcore.services.settings.constants import VARIABLES_TO_GET_FROM_ENVIRONMENT
from agentcore.utils.util_strings import is_valid_database_url

# BASE_COMPONENTS_PATH = str(Path(__file__).parent / "components")
BASE_COMPONENTS_PATH = str(Path(__file__).parent.parent.parent / "components")


def is_list_of_any(field: FieldInfo) -> bool:
    """Check if the given field is a list or an optional list of any type.

    Args:
        field (FieldInfo): The field to be checked.

    Returns:
        bool: True if the field is a list or a list of any type, False otherwise.
    """
    if field.annotation is None:
        return False
    try:
        union_args = field.annotation.__args__ if hasattr(field.annotation, "__args__") else []

        return field.annotation.__origin__ is list or any(
            arg.__origin__ is list for arg in union_args if hasattr(arg, "__origin__")
        )
    except AttributeError:
        return False


class MyCustomSource(EnvSettingsSource):
    @override
    def prepare_field_value(self, field_name: str, field: FieldInfo, value: Any, value_is_complex: bool) -> Any:  # type: ignore[misc]
        # allow comma-separated list parsing

        # fieldInfo contains the annotation of the field
        if is_list_of_any(field):
            if isinstance(value, str):
                value = value.split(",")
            if isinstance(value, list):
                return value

        return super().prepare_field_value(field_name, field, value, value_is_complex)


class Settings(BaseSettings):
    # Define the default AGENTCORE_DIR
    config_dir: str | None = None

    dev: bool = False
    """If True, Agentcore will run in development mode."""
    database_url: str | None = None
    """Database URL for Agentcore. Must be a PostgreSQL connection string.
    The driver `postgresql` will be automatically converted to the async driver
    `postgresql+psycopg`."""
    database_connection_retry: bool = False
    """If True, Agentcore will retry to connect to the database if it fails."""
    pool_size: int = 20
    """The number of connections to keep open in the connection pool.
    For high load scenarios, this should be increased based on expected concurrent users."""
    max_overflow: int = 30
    """The number of connections to allow that can be opened beyond the pool size.
    Should be 2x the pool_size for optimal performance under load."""

    db_driver_connection_settings: dict | None = None
    """Database driver connection settings."""

    db_connection_settings: dict | None = {
        "pool_size": 20,  # Match the pool_size above
        "max_overflow": 30,  # Match the max_overflow above
        "pool_timeout": 30,  # Seconds to wait for a connection from pool
        "pool_pre_ping": True,  # Check connection validity before using
        "pool_recycle": 1800,  # Recycle connections after 30 minutes
        "echo": False,  # Set to True for debugging only
    }
    """Database connection settings optimized for high load scenarios.

    Settings:
    - pool_size: Number of connections to maintain (increase for higher concurrency)
    - max_overflow: Additional connections allowed beyond pool_size
    - pool_timeout: Seconds to wait for an available connection
    - pool_pre_ping: Validates connections before use to prevent stale connections
    - pool_recycle: Seconds before connections are recycled (prevents timeouts)
    - echo: Enable SQL query logging (development only)
    """

    use_noop_database: bool = False
    """If True, disables all database operations and uses a no-op session.
    Controlled by AGENTCORE_USE_NOOP_DATABASE env variable."""

    # cache configuration
    #cache_type: Literal["async", "redis", "memory"] = "async"
    cache_type: Literal["async", "redis", "memory"] = "redis"
    """The cache type can be 'async', 'redis' or 'memory'. Default is 'redis' for distributed caching."""
    """The cache type can be 'async' or 'redis'."""
    redis_host: str = os.getenv("REDIS_HOST")
    redis_port: int =os.getenv("REDIS_PORT")
    redis_db: int = 0
    redis_url: str | None = None
    redis_password: str =os.getenv("REDIS_PASSWORD")
    redis_ssl: bool = os.getenv("REDIS_SSL")
    cache_expire: int = os.getenv("REDIS_CACHE_EXPIRE")
    redis_cache_expire: int = os.getenv("REDIS_CACHE_EXPIRE")
    stm_cache_ttl: int = int(os.getenv("STM_CACHE_TTL", "300"))
    """STM (Short Term Memory) cache TTL in seconds. Default is 300 (5 minutes)."""

    # LTM (Long Term Memory) settings — defaults from services/ltm/LTM_DEFAULTS
    ltm_enabled: bool = _LTM_D["enabled"]
    """Enable the Long Term Memory background processor."""
    ltm_message_threshold: int = _LTM_D["message_threshold"]
    """Trigger LTM processing after this many new messages per agent."""
    ltm_time_interval_minutes: int = _LTM_D["time_interval_minutes"]
    """Trigger LTM processing every N minutes (time-based trigger)."""
    ltm_embedding_provider: str = "openai"
    """Embedding provider for LTM: 'openai' or 'azure_openai'."""
    ltm_embedding_model: str = "text-embedding-3-small"
    """Embedding model name (OpenAI) or deployment name (Azure)."""
    ltm_embedding_api_key: str = ""
    """API key for the LTM embedding model."""
    ltm_azure_openai_endpoint: str = ""
    """Azure OpenAI endpoint URL (e.g., https://your-resource.openai.azure.com/)."""
    ltm_azure_openai_api_version: str = "2024-02-01"
    """Azure OpenAI API version."""
    ltm_embedding_dimensions: int = 0
    """Optional: Reduce embedding dimensions (e.g., 512 to match Pinecone index). 0 = use model default."""
    ltm_max_summary_tokens: int = _LTM_D["max_summary_tokens"]
    """Maximum tokens for LLM-generated conversation summaries."""
    ltm_max_context_chars: int = _LTM_D["max_context_chars"]
    """Maximum characters of LTM context prepended to user messages."""
    ltm_pinecone_top_k: int = _LTM_D["pinecone_top_k"]
    """Number of summaries to retrieve from Pinecone."""
    ltm_neo4j_top_k: int = _LTM_D["neo4j_top_k"]
    """Number of entities/relationships to retrieve from Neo4j."""
    ltm_llm_provider: str = ""
    """LLM provider for LTM summarization (e.g., groq, openai, azure). Uses model service."""
    ltm_llm_model: str = ""
    """LLM model name for LTM summarization (e.g., meta-llama/llama-4-scout-17b-16e-instruct)."""
    ltm_llm_registry_model_id: str = ""
    """Optional: Model Registry ID for LTM LLM. If set, provider/model are resolved from registry."""
    # LTM Pinecone (separate free-tier instance)
    ltm_pinecone_api_key: str = ""
    """Pinecone API key for LTM (separate from the main RAG Pinecone)."""
    ltm_pinecone_index: str = "ltm-summaries"
    """Pinecone index name for storing LTM conversation summaries."""
    ltm_pinecone_cloud: str = "aws"
    """Pinecone cloud provider for LTM index."""
    ltm_pinecone_region: str = "us-east-1"
    """Pinecone cloud region for LTM index."""
    # LTM Neo4j — uses NEO4J_* env vars directly
    ltm_neo4j_uri: str = Field(default="", validation_alias="NEO4J_URI")
    """Neo4j connection URI for LTM."""
    ltm_neo4j_username: str = Field(default="", validation_alias="NEO4J_USERNAME")
    """Neo4j username for LTM."""
    ltm_neo4j_password: str = Field(default="", validation_alias="NEO4J_PASSWORD")
    """Neo4j password for LTM."""
    ltm_neo4j_database: str = Field(default="neo4j", validation_alias="NEO4J_DATABASE")
    """Neo4j database name for LTM."""
    ltm_neo4j_graph_kb_id: str = "ltm"
    """Neo4j graph_kb_id for isolating LTM entities."""

    # Semantic Search settings
    semantic_search_enabled: bool = False
    """Enable semantic search across Projects, Agents, and Models."""
    semantic_search_pinecone_index: str = "semantic-search"
    """Pinecone index name for semantic search vectors."""
    semantic_search_embedding_dimensions: int = 1536
    """Embedding dimensions for semantic search (text-embedding-3-small default: 1536)."""

    """The cache expire in seconds."""
    # [VARIABLE REMOVED] variable_store setting removed — migrating to Azure Key Vault

    disable_track_apikey_usage: bool = False
    remove_api_keys: bool = False
    components_path: list[str] = []
    langchain_cache: str = "InMemoryCache"
    load_agents_path: str | None = None
    bundle_urls: list[str] = []

    # # Redis
    # redis_host: str = "agentcoreredis.redis.cache.windows.net"
    # redis_port: int = 6380
    # redis_db: int = 0
    # redis_url: str | None = None
    # redis_password: str | None = "7iQsiMysElkfTwCNsyAuiQng3Eeeat6jFAzCaCPfsQw="
    # redis_cache_expire: int = 3600


    storage_type: str = "local"
    azure_release_documents_container_name: str = Field(
        default="",
        validation_alias="AZURE_RELEASE_DOCUMENTS_CONTAINER_NAME",
    )

    fallback_to_env_var: bool = True
    """If set to True, Global Variables set in the UI will fallback to a environment variable
    with the same name in case Agentcore fails to retrieve the variable value."""

    # [VARIABLE REMOVED] store_environment_variables and variables_to_get_from_environment removed
    #Azure Key Vault
    worker_timeout: int = 300
    """Timeout for the API calls in seconds."""
    frontend_timeout: int = 0
    """Timeout for the frontend API calls in seconds."""
    user_agent: str = "agentcore"
    """User agent for the API calls."""
    backend_only: bool = False
    """If set to True, Agentcore will not serve the frontend."""

    # SMTP Email
    smtp_host: str = Field(default="", validation_alias="SMTP_HOST")
    smtp_server: str = Field(default="", validation_alias="SMTP_SERVER")
    mail_server: str = Field(default="", validation_alias="MAIL_SERVER")
    smtp_port: int = Field(default=0, validation_alias="SMTP_PORT")
    mail_port: int = Field(default=0, validation_alias="MAIL_PORT")
    smtp_username: str = Field(default="", validation_alias="SMTP_USERNAME")
    smtp_user: str = Field(default="", validation_alias="SMTP_USER")
    mail_username: str = Field(default="", validation_alias="MAIL_USERNAME")
    smtp_password: str = Field(default="", validation_alias="SMTP_PASSWORD")
    mail_password: str = Field(default="", validation_alias="MAIL_PASSWORD")
    smtp_from_email: str = Field(default="", validation_alias="SMTP_FROM_EMAIL")
    smtp_from: str = Field(default="", validation_alias="SMTP_FROM")
    mail_from: str = Field(default="", validation_alias="MAIL_FROM")
    mail_from_email: str = Field(default="", validation_alias="MAIL_FROM_EMAIL")
    smtp_from_name: str = Field(default="", validation_alias="SMTP_FROM_NAME")
    mail_from_name: str = Field(default="", validation_alias="MAIL_FROM_NAME")
    smtp_use_tls: bool = Field(default=True, validation_alias="SMTP_USE_TLS")
    mail_use_tls: bool = Field(default=True, validation_alias="MAIL_USE_TLS")
    smtp_use_ssl: bool = Field(default=False, validation_alias="SMTP_USE_SSL")
    mail_use_ssl: bool = Field(default=False, validation_alias="MAIL_USE_SSL")
    smtp_timeout_seconds: int = Field(default=20, validation_alias="SMTP_TIMEOUT_SECONDS")

    # Telemetry
    do_not_track: bool = True
    """If set to True, Agentcore will not track telemetry."""
    telemetry_base_url: str = os.getenv("LOCALHOST_TELEMETRY_BASE_URL")  # Disabled endpoint
    transactions_storage_enabled: bool = True
    """If set to True, Agentcore will track transactions between agents."""
    vertex_builds_storage_enabled: bool = True
    """If set to True, Agentcore will keep track of each vertex builds (outputs) in the UI for any agent."""

    # Config
    host: str = os.getenv("LOCALHOST_HOST")
    """The host on which Agentcore will run."""
    port: int = os.getenv("BACKEND_PORT")
    """The port on which Agentcore will run."""
    workers: int = 1
    """The number of workers to run."""
    log_level: str = "critical"
    """The log level for Agentcore."""
    log_file: str | None = "logs/agentcore.log"
    """The path to log file for Agentcore."""
    alembic_log_file: str = "alembic/alembic.log"
    """The path to log file for Alembic for SQLAlchemy."""
    frontend_path: str | None = None
    """The path to the frontend directory containing build files. This is for development purposes only.."""
    auto_saving: bool = True
    """If set to True, Agentcore will auto save agents."""
    auto_saving_interval: int = 1000
    """The interval in ms at which Agentcore will auto save agents."""
    health_check_max_retries: int = 5
    """The maximum number of retries for the health check."""
    max_file_size_upload: int = 1024
    """The maximum file size for the upload in MB."""
    deactivate_tracing: bool = False
    """If set to True, tracing will be deactivated."""
    max_transactions_to_keep: int = 3000
    """The maximum number of transactions to keep in the database."""
    max_vertex_builds_to_keep: int = 3000
    """The maximum number of vertex builds to keep in the database."""
    max_vertex_builds_per_vertex: int = 2
    """The maximum number of builds to keep per vertex. Older builds will be deleted."""
    ssl_cert_file: str | None = None
    """Path to the SSL certificate file on the local system."""
    ssl_key_file: str | None = None
    """Path to the SSL key file on the local system."""
    max_text_length: int = MAX_TEXT_LENGTH
    """Maximum number of characters to store and display in the UI. Responses longer than this
    will be truncated when displayed in the UI. Does not truncate responses between components nor outputs."""
    max_items_length: int = MAX_ITEMS_LENGTH
    """Maximum number of items to store and display in the UI. Lists longer than this
    will be truncated when displayed in the UI. Does not affect data passed between components nor outputs."""

    # MCP Server
    mcp_server_enabled: bool = True
    """If set to False, Agentcore will not enable the MCP server."""
    mcp_server_enable_progress_notifications: bool = False
    """If set to False, Agentcore will not send progress notifications in the MCP server."""

    # Backend Service API Key (for cross-region gateway calls via x-api-key)
    backend_service_api_key: str = ""
    """API key for authenticating service-to-service calls to this backend (e.g. region-gateway → backend).
    Resolved from Key Vault secret 'agentcore-backend-service-api-key'. When set, incoming requests
    with a matching x-api-key header are authenticated as a service caller without requiring JWT."""

    # Model Microservice
    model_service_url: str = ""
    """Base URL of the Model microservice (e.g. http://localhost:8001).
    When set, registry operations and LLM/embedding invocations are proxied through the microservice."""
    model_service_api_key: str = ""
    """API key for authenticating with the Model microservice (sent as x-api-key header)."""

    # MCP Microservice
    mcp_service_url: str = ""
    """Base URL of the MCP microservice (e.g. http://localhost:8002).
    When set, MCP registry operations, tool discovery, and tool invocations are proxied through the microservice."""
    mcp_service_api_key: str = ""
    """API key for authenticating with the MCP microservice (sent as x-api-key header)."""

    # Guardrails Microservice
    guardrails_service_url: str = ""
    """Base URL of the Guardrails microservice (e.g. http://localhost:8003).
    When set, guardrail catalogue CRUD and NeMo guardrail execution are proxied through the microservice."""
    guardrails_service_api_key: str = ""
    """API key for authenticating with the Guardrails microservice (sent as x-api-key header)."""
    # Pinecone Microservice
    pinecone_service_url: str = ""
    """Base URL of the Pinecone microservice (e.g. http://localhost:8003).
    When set, Pinecone vector store operations are proxied through the microservice."""
    pinecone_service_api_key: str = ""
    """API key for authenticating with the Pinecone microservice (sent as x-api-key header)."""

    # Azure AI Search (direct SDK — no microservice needed)
    azure_ai_search_endpoint: str = ""
    """Azure AI Search service endpoint (e.g. https://mysearch.search.windows.net).
    The component uses the azure-search-documents SDK directly."""
    azure_ai_search_api_key: str = ""
    """Azure AI Search admin API key for index management, ingestion, and search."""

    # Unified RAG Microservice (Pinecone + Graph RAG)
    rag_service_url: str = ""
    """Base URL of the unified RAG microservice (e.g. http://localhost:8005).
    When set, both Pinecone and Neo4j operations are proxied through this service."""
    rag_service_api_key: str = ""
    """API key for authenticating with the RAG microservice (sent as x-api-key header)."""

    # Graph RAG Microservice (legacy — use rag_service_url instead)
    graph_rag_service_url: str = ""
    """Base URL of the Graph RAG microservice (e.g. http://localhost:8004).
    When set, Neo4j graph operations are proxied through the microservice."""
    graph_rag_service_api_key: str = ""
    """API key for authenticating with the Graph RAG microservice (sent as x-api-key header)."""

    # Manifest
    manifest_file_path: str = ""
    """Absolute or relative path to the agents.yaml file used by the publish/notify flow.
    When empty, defaults to <project_root>/agents.yaml."""

    # Git manifest sync
    git_provider: str = ""
    """Which Git provider(s) to push the manifest to.
    Accepted values: 'github', 'ado', 'both'.
    Leave empty to disable remote git sync entirely."""
    github_repo_url: str = ""
    """GitHub repo URL — required when git_provider is 'github' or 'both'.
    Example: https://github.com/owner/repo"""
    github_token: str = ""
    """PAT token with Contents read/write access for the GitHub repo."""
    ado_repo_url: str = ""
    """Azure DevOps repo URL — required when git_provider is 'ado' or 'both'.
    Example: https://dev.azure.com/org/project/_git/repo"""
    ado_token: str = ""
    """PAT token with repo write access for the Azure DevOps repo."""
    git_branch: str = "main"
    """Branch to commit the manifest file to (applies to both providers)."""
    git_manifest_file: str = "agents.yaml"
    """Path of the manifest file inside the repo (e.g. 'helm-chart/agents.yaml').
    Applies to both providers."""

    # Public Agent Settings
    public_agent_cleanup_interval: int = Field(default=3600, gt=600)
    """The interval in seconds at which public temporary agents will be cleaned up.
    Default is 1 hour (3600 seconds). Minimum is 600 seconds (10 minutes)."""
    public_agent_expiration: int = Field(default=86400, gt=600)
    """The time in seconds after which a public temporary agent will be considered expired and eligible for cleanup.
    Default is 24 hours (86400 seconds). Minimum is 600 seconds (10 minutes)."""
    event_delivery: Literal["polling", "streaming", "direct"] = "streaming"
    """How to deliver build events to the frontend. Can be 'polling', 'streaming' or 'direct'."""
    lazy_load_components: bool = False
    """If set to True, Agentcore will only partially load components at startup and fully load them on demand.
    This significantly reduces startup time but may cause a slight delay when a component is first used."""

    # Starter Projects
    # Microsoft Teams Bot Integration
    teams_bot_app_id: str | None = None
    """Azure AD App ID for the Teams bot registration."""
    teams_bot_app_secret: str | None = None
    """Azure AD App Secret for the Teams bot registration."""
    teams_bot_tenant_id: str | None = None
    """Azure AD Tenant ID for Teams bot. Defaults to AZURE_TENANT_ID if not set."""
    teams_graph_client_id: str | None = None
    """Azure AD App ID for Microsoft Graph API access (app catalog management).
    Can be the same as teams_bot_app_id if permissions are combined."""
    teams_graph_client_secret: str | None = None
    """Azure AD App Secret for Microsoft Graph API access."""
    teams_bot_endpoint_base: str | None = None
    """Public base URL for the bot messaging endpoint, e.g. https://agentcore.yourcompany.com"""
    teams_graph_redirect_uri: str | None = None
    """OAuth redirect URI for Microsoft Graph delegated auth.
    Defaults to http://localhost:{BACKEND_PORT}/api/teams/oauth/callback"""

    create_starter_projects: bool = True
    """If set to True, Agentcore will create starter projects. If False, skips all starter project setup.
    Note that this doesn't check if the starter projects are already loaded in the db;
    this is intended to be used to skip all startup project logic."""
    update_starter_projects: bool = True
    """If set to True, Agentcore will update starter projects."""

    @field_validator("use_noop_database", mode="before")
    @classmethod
    def set_use_noop_database(cls, value):
        if value:
            logger.info("Running with NOOP database session. All DB operations are disabled.")
        return value

    @field_validator("event_delivery", mode="before")
    @classmethod
    def set_event_delivery(cls, value, info):
        workers = int(info.data.get("workers", 1) or 1)

        # If workers > 1, we need direct delivery because polling/streaming
        # rely on in-process state and are not safe across workers.
        if workers > 1:
            logger.warning("Multi-worker environment detected; forcing direct event delivery")
            return "direct"

        return value

    @field_validator("dev")
    @classmethod
    def set_dev(cls, value):
        from agentcore.settings import set_dev

        set_dev(value)
        return value

    @field_validator("user_agent", mode="after")
    @classmethod
    def set_user_agent(cls, value):
        if not value:
            value = "Agentcore"
        import os

        os.environ["USER_AGENT"] = value
        logger.debug(f"Setting user agent to {value}")
        return value

    # [VARIABLE REMOVED] variables_to_get_from_environment validator removed

    @field_validator("log_file", mode="before")
    @classmethod
    def set_log_file(cls, value):
        if isinstance(value, Path):
            value = str(value)
        return value

    @field_validator("config_dir", mode="before")
    @classmethod
    def set_agentcore_dir(cls, value):
        backend_root = Path(__file__).resolve().parents[4]
        project_root = backend_root.parent.parent

        if not value:
            # Default storage root for uploaded knowledge/files inside the backend tree.
            value = backend_root / "knowledge_base_storage"
            value.mkdir(parents=True, exist_ok=True)

        if isinstance(value, str):
            value = Path(value)

        # For relative values, always anchor to the project root so behavior is
        # independent of the process working directory.
        if not value.is_absolute():
            value = (project_root / value).resolve()

        if not value.exists():
            value.mkdir(parents=True, exist_ok=True)

        return str(value.resolve())

    @field_validator("database_url", mode="before")
    @classmethod
    def set_database_url(cls, value, info):
        if value and not is_valid_database_url(value):
            msg = f"Invalid database_url provided: '{value}'"
            raise ValueError(msg)


        if agentcore_database_url := os.getenv("DATABASE_URL"):
            value = agentcore_database_url
            logger.debug("Using AGENTCORE_DATABASE_URL env variable.")
        else:
            msg = "No DATABASE_URL environment variable set. PostgreSQL is required."
            raise ValueError(msg)

        return value

    @field_validator("components_path", mode="before")
    @classmethod
    def set_components_path(cls, value):
        """Processes and updates the components path list, incorporating environment variable overrides.

        If the `AGENTCORE_COMPONENTS_PATH` environment variable is set and points to an existing path, it is
        appended to the provided list if not already present. If the input list is empty or missing, it is
        set to an empty list.
        """
        if os.getenv("COMPONENTS_PATH"):
            logger.debug("Adding AGENTCORE_COMPONENTS_PATH to components_path")
            agentcore_component_path = os.getenv("COMPONENTS_PATH")
            if Path(agentcore_component_path).exists() and agentcore_component_path not in value:
                if isinstance(agentcore_component_path, list):
                    for path in agentcore_component_path:
                        if path not in value:
                            value.append(path)
                    logger.debug(f"Extending {agentcore_component_path} to components_path")
                elif agentcore_component_path not in value:
                    value.append(agentcore_component_path)
                    logger.debug(f"Appending {agentcore_component_path} to components_path")

        if not value:
            value = [BASE_COMPONENTS_PATH]
            logger.debug("Setting default components path to components_path")
        else:
            if isinstance(value, Path):
                value = [str(value)]
            elif isinstance(value, list):
                value = [str(p) if isinstance(p, Path) else p for p in value]
            logger.debug("Adding default components path to components_path")

        logger.debug(f"Components path: {value}")
        return value

    model_config = SettingsConfigDict(validate_assignment=True, extra="ignore", env_prefix="")

    async def update_from_yaml(self, file_path: str, *, dev: bool = False) -> None:
        new_settings = await load_settings_from_yaml(file_path)
        self.components_path = new_settings.components_path or []
        self.dev = dev

    def update_settings(self, **kwargs) -> None:
        logger.debug("Updating settings")
        for key, value in kwargs.items():
            # value may contain sensitive information, so we don't want to log it
            if not hasattr(self, key):
                logger.debug(f"Key {key} not found in settings")
                continue
            logger.debug(f"Updating {key}")
            if isinstance(getattr(self, key), list):
                # value might be a '[something]' string
                value_ = value
                with contextlib.suppress(json.decoder.JSONDecodeError):
                    value_ = orjson.loads(str(value))
                if isinstance(value_, list):
                    for item in value_:
                        item_ = str(item) if isinstance(item, Path) else item
                        if item_ not in getattr(self, key):
                            getattr(self, key).append(item_)
                    logger.debug(f"Extended {key}")
                else:
                    value_ = str(value_) if isinstance(value_, Path) else value_
                    if value_ not in getattr(self, key):
                        getattr(self, key).append(value_)
                        logger.debug(f"Appended {key}")

            else:
                setattr(self, key, value)
                logger.debug(f"Updated {key}")
            logger.debug(f"{key}: {getattr(self, key)}")

    @classmethod
    @override
    def settings_customise_sources(  # type: ignore[misc]
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (MyCustomSource(settings_cls),)

async def load_settings_from_yaml(file_path: str) -> Settings:
    # Check if a string is a valid path or a file name
    if "/" not in file_path:
        # Get current path
        current_path = Path(__file__).resolve().parent
        file_path_ = Path(current_path) / file_path
    else:
        file_path_ = Path(file_path)

    async with async_open(file_path_.name, encoding="utf-8") as f:
        content = await f.read()
        settings_dict = yaml.safe_load(content)
        settings_dict = {k.upper(): v for k, v in settings_dict.items()}

        for key in settings_dict:
            if key not in Settings.model_fields:
                msg = f"Key {key} not found in settings"
                raise KeyError(msg)
            logger.debug(f"Loading {len(settings_dict[key])} {key} from {file_path}")

    return await asyncio.to_thread(Settings, **settings_dict)
