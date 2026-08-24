from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "workflow-benchmark"
    database_url: str = "postgresql+psycopg2://benchmark:benchmark@localhost:5432/benchmark"

    engine_operaton_url: str = "http://localhost:8080/engine-rest"
    engine_flowable_url: str = "http://localhost:8081/flowable-rest/service"

    outbox_dispatcher_enabled: bool = True
    dispatch_interval_seconds: float = 2.0
    dispatch_max_attempts: int = 5

    reconciler_enabled: bool = True
    reconcile_interval_seconds: float = 2.0

    default_workflow_engine: str = "OPERATON"


@lru_cache
def get_settings() -> Settings:
    return Settings()
