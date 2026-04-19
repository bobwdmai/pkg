import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OllamaConfig:
    local_endpoint: str = "http://127.0.0.1:11434"
    remote_endpoint: str = "http://10.0.0.133:11434"
    fast_model: str = "qwen2.5-coder:3b"
    fallback_model: str = "qwen2.5-coder:1.5b"
    heavy_model: str = "qwen2.5-coder:14b"


class OllamaClient:
    def __init__(self, config: Optional[OllamaConfig] = None) -> None:
        self.config = config or OllamaConfig()

    def _model_target(self, key: str) -> tuple[str, str]:
        if key == "fallback":
            return self.config.local_endpoint, self.config.fallback_model
        if key == "fast":
            return self.config.local_endpoint, self.config.fast_model
        if key == "heavy":
            return self.config.remote_endpoint, self.config.heavy_model
        return self.config.local_endpoint, self.config.fast_model

    def _first_enabled(self, preferred: list[str], enabled_models: dict[str, bool]) -> Optional[str]:
        for key in preferred:
            if enabled_models.get(key, False):
                return key
        return None

    def resolve_target(
        self,
        prompt: str,
        mode: str,
        enabled_models: Optional[dict[str, bool]] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[str], str]:
        enabled = enabled_models or {"fallback": True, "fast": True, "heavy": True}

        if not any(enabled.values()):
            return None, None, None, "no-model-enabled"

        selected_key: Optional[str]
        if mode == "manual-fast":
            selected_key = self._first_enabled(["fast", "fallback", "heavy"], enabled)
        elif mode == "manual-fallback":
            selected_key = self._first_enabled(["fallback", "fast", "heavy"], enabled)
        elif mode == "manual-heavy":
            selected_key = self._first_enabled(["heavy", "fast", "fallback"], enabled)
        else:
            lowered = prompt.lower()
            complex_markers = (
                "refactor",
                "architecture",
                "multi-file",
                "optimize",
                "debug",
                "test strategy",
                "migration",
                "production",
            )
            preferred = "fast"
            if len(prompt) > 900 or any(marker in lowered for marker in complex_markers):
                preferred = "heavy"
            elif len(prompt) < 180:
                preferred = "fallback"

            if preferred == "heavy":
                selected_key = self._first_enabled(["heavy", "fast", "fallback"], enabled)
            elif preferred == "fallback":
                selected_key = self._first_enabled(["fallback", "fast", "heavy"], enabled)
            else:
                selected_key = self._first_enabled(["fast", "fallback", "heavy"], enabled)

        if not selected_key:
            return None, None, None, "no-model-enabled"

        endpoint, model = self._model_target(selected_key)
        return endpoint, model, selected_key, selected_key

    def chat_to(self, endpoint: str, model: str, prompt: str, timeout: int = 120) -> str:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        req = urllib.request.Request(
            f"{endpoint}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                data = json.loads(body)
                return data.get("response", "").strip() or "[empty model response]"
        except urllib.error.HTTPError as exc:
            return f"[ollama http error] {exc.code} {exc.reason}"
        except urllib.error.URLError as exc:
            return f"[ollama connection error] {exc.reason} (endpoint={endpoint}, model={model})"
        except TimeoutError:
            return f"[ollama timeout] request exceeded {timeout}s"
        except Exception as exc:  # pragma: no cover
            return f"[ollama unexpected error] {exc}"
