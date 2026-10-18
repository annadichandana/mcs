import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
load_dotenv(PROJECT / ".env")
load_dotenv(BACKEND / ".env")


class Settings:
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    fmp_api_key: str = os.getenv("FMP_API_KEY", "")
    anthropic_workspace_id: str = os.getenv("ANTHROPIC_WORKSPACE_ID", "")
    model: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
    # LLM_BACKEND: "auto" (API key if set, else the `claude` CLI), "api", or "cli"
    llm_backend: str = os.getenv("LLM_BACKEND", "auto")
    claude_cli_path: str = os.getenv("CLAUDE_CLI_PATH", "")
    claude_cli_model: str = os.getenv("CLAUDE_CLI_MODEL", "")   # e.g. "sonnet"; empty = CLI default
    data_dir: Path = Path(os.getenv("DATA_DIR", PROJECT / "data")).resolve()
    chunk_chars: int = int(os.getenv("CHUNK_CHARS", "80000"))

    @property
    def ledger_dir(self) -> Path:
        return self.data_dir / "ledger"

    @property
    def transcript_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def ensure_dirs(self) -> None:
        for d in (self.ledger_dir, self.transcript_dir, self.results_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
