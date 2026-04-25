from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openrouter_api_key: str
    proxy_api_key: str

    models_list_url: str = "https://shir-man.com/api/free-llm/top-models"
    models_refresh_sec: int = 3600

    upstream_base_url: str = "https://openrouter.ai/api/v1"
    upstream_timeout_sec: float = 30.0
    max_fallback_attempts: int = 5

    rate_limit_cooldown_sec: int = 300
    generic_error_cooldown_sec: int = 60

    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8080

    openrouter_referer: str = "https://github.com/promsoft/free-llm-proxy"
    openrouter_title: str = "free-llm-proxy"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


def reset_settings_cache() -> None:
    global _settings
    _settings = None
