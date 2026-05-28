"""Configuration via environment variables."""

import os
from pathlib import Path


class Settings:
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")
    neo4j_uri: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    neo4j_user: str = os.getenv("NEO4J_USER", "neo4j")
    neo4j_password: str = os.getenv("NEO4J_PASSWORD", "")  # override via env in production
    me_name: str = os.getenv("ME_NAME", "agent")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    gen_model: str = os.getenv("GEN_MODEL", "qwen3:8b")
    data_dir: str = os.getenv("DATA_DIR", str(Path.cwd() / "data"))


settings = Settings()
