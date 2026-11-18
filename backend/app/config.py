"""Configuración central: lee variables desde el archivo .env de la raíz del repo.

pydantic-settings valida los tipos al arrancar: si falta DATABASE_URL,
la app falla de inmediato con un error claro (mejor que fallar a mitad de una request).
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    database_url: str
    voyage_api_key: str
    anthropic_api_key: str


settings = Settings()
