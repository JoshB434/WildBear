import os
from dataclasses import dataclass


@dataclass
class Settings:
    alpaca_api_key_id: str | None = "PKD7FPV7WNLAFFANMC4FLHXPGD"
    alpaca_api_secret_key: str | None = "oyD2KuEC2J9WYoRgRs7xRQuToxJ4vmCjExmZsnf7HGp"
    alpaca_base_url: str | None = "https://paper-api.alpaca.markets/v2"
    tradingview_webhook_secret: str | None = os.getenv("TRADINGVIEW_WEBHOOK_SECRET")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")


settings = Settings()
