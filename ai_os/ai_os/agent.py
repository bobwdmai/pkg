from dataclasses import dataclass
from typing import Optional

from .ollama_client import OllamaClient


SYSTEM_PROMPT = """You are AI OS, a pragmatic coding assistant.
Rules:
1) Keep answers concise and actionable.
2) If code is requested, provide runnable code blocks.
3) Prefer safe local-first workflows.
4) Explain assumptions briefly.
"""


@dataclass
class AgentResult:
    prompt: str
    response: str
    used_mode: str


class AiderStyleAgent:
    def __init__(self, client: Optional[OllamaClient] = None) -> None:
        self.client = client or OllamaClient()

    def run(
        self,
        user_prompt: str,
        mode: str = "auto",
        enabled_models: Optional[dict[str, bool]] = None,
        model_roles: Optional[dict[str, str]] = None,
        interruption_note: str = "",
    ) -> AgentResult:
        endpoint, model, model_key, routed_mode = self.client.resolve_target(
            prompt=user_prompt,
            mode=mode,
            enabled_models=enabled_models,
        )

        if not endpoint or not model or not model_key:
            return AgentResult(
                prompt=user_prompt,
                response="No model is enabled. Open Settings and enable at least one model.",
                used_mode="no-model-enabled",
            )

        role_instruction = ""
        if model_roles:
            role_instruction = model_roles.get(model_key, "").strip()

        assembled_prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"Active model profile: {model_key}\n"
            f"Profile instruction: {role_instruction or '[default behavior]'}\n"
            f"Interruption context: {interruption_note or '[none]'}\n\n"
            f"User request:\n{user_prompt}\n\n"
            "Respond now:"
        )

        response = self.client.chat_to(endpoint=endpoint, model=model, prompt=assembled_prompt)
        used = f"{mode}->{routed_mode}"
        return AgentResult(prompt=user_prompt, response=response, used_mode=used)
