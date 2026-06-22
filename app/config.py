"""Configuration via environment variables + .env file."""
from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    browser_use_api_key: str = ""
    neon_database_url: str = ""
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    database_path: str = "fcn.db"
    session_secret: str = "change-me-in-production"
    admin_username: str = "admin"
    admin_password: str = "changeme"
    log_level: str = "INFO"
    capsolver_api_key: str = ""      # solves reCAPTCHA/Turnstile (NOT hCaptcha — CapSolver dropped it)
    twocaptcha_api_key: str = ""     # solves FCN's in-chat hCaptcha (CapSolver can't)
    # Block image/media/font downloads on the bot pages. These resource types are the bulk
    # of Browser Use proxy bandwidth ($5/GB) and the bots never need to SEE them (they read
    # text + send photos from base64, neither of which loads inbound media). Set BLOCK_MEDIA=false
    # to disable if a page misbehaves.
    block_media: bool = True
    # Block ALL third-party hosts (everything not freechatnow.com / Cloudflare). FCN's ad scripts
    # spawn popunders that pull huge payloads from ad CDNs (e.g. proof.ovh.net) — measured at ~90%
    # of proxy bandwidth, dwarfing the chat site itself. Applied at the browser-CONTEXT level so it
    # also covers popup/popunder tabs (page-level routes don't). Set BLOCK_THIRDPARTY=false to
    # disable if Cloudflare login breaks.
    block_thirdparty: bool = True
    # Seconds between a bot's group-room broadcasts. Was a hardcoded 10-20s, which broadcast
    # ~4x/min and tripped FCN's anti-spam hCaptcha fast. Slower + wider variance = far fewer
    # captchas (less CapSolver spend) at a small exposure cost. Tune via env if needed.
    broadcast_min_s: int = 30
    broadcast_max_s: int = 60

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

settings = Settings()