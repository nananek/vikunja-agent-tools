"""環境変数 / .env から設定を読み込む。"""

from __future__ import annotations

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """設定の読み込みに失敗したときに送出する、利用者向けの分かりやすい例外。"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    vikunja_base_url: str
    vikunja_api_token: SecretStr
    vikunja_project_id: str | None = None
    vikunja_timeout_seconds: float = 15
    vikunja_verify_tls: bool = True
    vikunja_agent_label_prefix: str = "agent-"
    vikunja_status_label_prefix: str = "status-"

    # .env.example には含めない任意設定 (コード側デフォルトのみで足りるが、環境変数での
    # 上書きは可能にする)。
    vikunja_heartbeat_interval_seconds: int = 900
    vikunja_stale_running_minutes: int = 30

    @property
    def base_url(self) -> str:
        return self.vikunja_base_url.rstrip("/")


def load_settings() -> Settings:
    """`Settings` を構築する。

    `VIKUNJA_API_TOKEN` 等の必須値が未設定の場合、pydantic の生の
    `ValidationError` ではなく `ConfigError` を送出し、原因が伝わるメッセージにする。
    """
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        missing = [
            str(error["loc"][0])
            for error in exc.errors()
            if error["type"] == "missing" and error["loc"]
        ]
        if missing:
            fields = ", ".join(missing)
            raise ConfigError(
                f"必須の環境変数が設定されていません: {fields}。"
                " .env (.env.example を参考に作成) または環境変数を確認してください。"
            ) from None
        raise ConfigError(
            f"設定の読み込みに失敗しました: {exc}"
        ) from None
