"""Ollama 本地模型 HTTP 客户端."""

from __future__ import annotations

import logging
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


class LocalLLM:
    """Ollama HTTP API 客户端.

    Args:
        config: 应用配置字典，从 config.yaml 读取。
    """

    def __init__(self, config: dict) -> None:
        models_config = config.get("models", {}).get("local", {})
        self.model = models_config.get("model", "qwen2.5:7b")
        self.base_url = models_config.get("base_url", "http://localhost:11434")
        self.timeout = models_config.get("timeout", 30)
        self.logger = LOGGER

    def generate(self, prompt: str, system: str = "") -> str:
        """调用 Ollama 生成接口.

        Args:
            prompt: 用户输入文本。
            system: 系统提示词。

        Returns:
            模型生成的文本，失败返回空字符串。
        """
        try:
            payload: dict[str, Any] = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0},
            }
            if system:
                payload["system"] = system

            resp = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            result = resp.json().get("response", "").strip()
            self.logger.debug("LocalLLM response: %s", result)
            return result

        except requests.Timeout:
            self.logger.warning("Ollama request timed out after %ds", self.timeout)
            return ""
        except requests.ConnectionError:
            self.logger.warning("Ollama not reachable at %s", self.base_url)
            return ""
        except ValueError:
            self.logger.error("Invalid JSON response from Ollama")
            return ""
        except Exception as exc:
            self.logger.error("Unexpected LocalLLM error: %s", exc)
            return ""

    def is_available(self) -> bool:
        """检查 Ollama 是否可用."""
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=3)
            return resp.status_code == 200
        except Exception as exc:
            self.logger.debug("Ollama availability check failed: %s", exc)
            return False
