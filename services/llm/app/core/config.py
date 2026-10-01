import os

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_VISION_MODEL = os.getenv("OLLAMA_VISION_MODEL", "moondream:latest")
OLLAMA_TIMEOUT_SECONDS = 2.0
OLLAMA_GENERATE_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_GENERATE_TIMEOUT_SECONDS", "60.0"))
# Keeps a pulled model resident in Ollama between requests so a burst of
# analyze-image calls doesn't repeatedly pay the ~55s cold-start cost.
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
