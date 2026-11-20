"""Settings read from environment variables (or a local .env file), and logging setup."""

import logging

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # .env is for local development only; in production the host sets real env vars.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Matches docker-compose.yml so local dev works with no .env at all.
    database_url: str = "postgresql://llms:llms@localhost:5432/llms"

    # Browser origins allowed to call the API, comma-separated, e.g. "https://app.example.com".
    # A plain string rather than list[str]: pydantic-settings would expect a JSON array for a
    # list, which is awkward to type into a host's env var settings.
    cors_origins: str = ""

    # Run the job worker inside the API process (see app.worker.main for the run modes). Off by
    # default, so local development and tests keep the API and worker separate.
    embedded_worker: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()


def configure_logging() -> None:
    """Send the app's INFO logs to stderr, where the host collects them.

    Needed in the API too: the embedded worker logs there, and uvicorn only configures its
    own loggers. basicConfig does nothing if logging is already set up, so calling it twice
    is harmless.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # The HTTP client logs every request at INFO: one line per crawled page would bury the
    # worker's own messages. Its warnings and errors still come through.
    for noisy in ("httpx2", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
