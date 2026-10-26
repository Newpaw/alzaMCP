"""Environment-only application configuration."""

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    alza_client_id: SecretStr = SecretStr("")
    alza_client_secret: SecretStr = SecretStr("")
    alza_scope: str = "marketplace_public"
    alza_base_url: str = "https://portalapi.alza.cz"
    alza_token_url: str = "https://identity.alza.cz/connect/token"
    alza_language: str = "cs"
    alza_timeout_seconds: float = Field(20, ge=1, le=60)
    alza_max_retries: int = Field(2, ge=0, le=3)
    alza_max_response_bytes: int = Field(20_000_000, ge=1024, le=100_000_000)
    mcp_host: str = "0.0.0.0"
    mcp_port: int = Field(8000, ge=1, le=65535)
    mcp_transport: str = "http"
    mcp_api_key: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def validate_config(self):
        if bool(self.alza_client_id.get_secret_value()) != bool(
            self.alza_client_secret.get_secret_value()
        ):
            raise ValueError("Set both ALZA_CLIENT_ID and ALZA_CLIENT_SECRET, or neither")
        if self.mcp_transport not in {"http", "stdio"}:
            raise ValueError("MCP_TRANSPORT must be http or stdio")
        for url in (self.alza_base_url, self.alza_token_url):
            if not url.startswith("https://"):
                raise ValueError("Alza API and token URLs must use HTTPS")
        return self

    @property
    def configured(self) -> bool:
        return bool(self.alza_client_id.get_secret_value())
