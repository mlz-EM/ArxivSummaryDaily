"""Shared Gemini client with bounded retries and cross-process deferral."""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from config.settings import LLM_CONFIG


class LLMQuotaExhausted(RuntimeError):
    """Further generation must wait for the provider's quota reset."""


class LLMContextLimit(RuntimeError):
    """Retry this workload in smaller batches, without discarding any jobs."""


class LLMServiceUnavailable(RuntimeError):
    """The provider is overloaded; preserve work and let the service recover."""


class LLMModelClient:
    def __init__(self, api_key: str, model: Optional[str] = None):
        self.api_key = api_key
        self.model = model or LLM_CONFIG["model"]
        self.api_url = f"{LLM_CONFIG['api_url']}/{self.model}:generateContent"
        self.timeout = LLM_CONFIG.get("timeout", 600)
        self.stop_reason = ""
        self._deferred_error = LLMQuotaExhausted

    def _create_request_body(self, messages, temperature=None, max_tokens=None):
        generation_config = {"maxOutputTokens": max_tokens or LLM_CONFIG["max_output_tokens"]}
        # Gemini 3 and later reject legacy sampling parameters.
        if not self.model.startswith(("gemini-3", "gemini-4")):
            generation_config.update({
                "temperature": LLM_CONFIG["temperature"] if temperature is None else temperature,
                "topP": LLM_CONFIG["top_p"],
                "topK": LLM_CONFIG["top_k"],
            })
        return {
            "contents": [{"parts": [{"text": messages[-1]["content"]}]}],
            "generationConfig": generation_config,
        }

    @staticmethod
    def _quota_path():
        value = os.getenv("LLM_QUOTA_STATE_FILE", "")
        return Path(value) if value else None

    def _check_deferred(self):
        if self.stop_reason:
            raise self._deferred_error(self.stop_reason)
        path = self._quota_path()
        if path and path.exists():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                reset_at = datetime.fromisoformat(state["retryAt"])
                if state["model"] == self.model and reset_at > datetime.now(timezone.utc):
                    self.stop_reason = str(state["reason"])
                    if state.get("errorType") == "LLMServiceUnavailable":
                        self._deferred_error = LLMServiceUnavailable
                    raise self._deferred_error(self.stop_reason)
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def _defer_requests(self, reason, retry_after, error_type=LLMQuotaExhausted):
        self.stop_reason = reason
        self._deferred_error = error_type
        path = self._quota_path()
        if path:
            reset_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, retry_after))
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps({
                "model": self.model, "reason": reason, "retryAt": reset_at.isoformat(),
                "errorType": error_type.__name__,
            }), encoding="utf-8")
            temporary.replace(path)
        raise error_type(reason)

    @staticmethod
    def _error_details(response):
        try:
            error = response.json().get("error", {})
        except (ValueError, AttributeError):
            error = {}
        message = str(error.get("message") or f"HTTP {response.status_code}")
        quota_ids, retry_after = [], 0.0
        for detail in error.get("details", []):
            for violation in detail.get("violations", []):
                quota_ids.append(str(violation.get("quotaId") or ""))
            try:
                retry_after = max(retry_after, float(str(detail.get("retryDelay", "0")).rstrip("s")))
            except ValueError:
                pass
        try:
            retry_after = max(retry_after, float(response.headers.get("Retry-After", 0)))
        except (ValueError, TypeError):
            pass
        return message, " ".join(quota_ids), retry_after

    def chat_completion(self, messages, temperature=None, max_tokens=None):
        self._check_deferred()
        data = self._create_request_body(messages, temperature, max_tokens)
        attempts = max(1, int(LLM_CONFIG.get("retry_count", 3)))
        for attempt in range(attempts):
            try:
                response = requests.post(
                    self.api_url,
                    headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
                    json=data, timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt + 1 == attempts:
                    raise RuntimeError(f"LLM network request failed after {attempts} attempts") from None
                time.sleep(min(60, LLM_CONFIG["retry_delay"] * (2**attempt)))
                continue
            if response.status_code != 200:
                message, quota_ids, retry_after = self._error_details(response)
                daily = "perday" in quota_ids.lower() or "per day" in message.lower() or "daily" in message.lower()
                if response.status_code == 429 and (daily or retry_after > 60):
                    self._defer_requests(
                        f"{self.model} quota exhausted; remaining work deferred. {message[:600]}",
                        retry_after or 86400,
                    )
                if response.status_code == 413 or (response.status_code == 400 and any(
                    text in message.lower() for text in ("token", "context", "too large", "payload")
                )):
                    raise LLMContextLimit(message[:600])
                if response.status_code == 429 and attempt + 1 == attempts:
                    self._defer_requests(
                        f"{self.model} rate limit persisted after {attempts} attempts; remaining work deferred.",
                        retry_after or 60,
                    )
                if response.status_code in {500, 502, 503, 504} and attempt + 1 == attempts:
                    self._defer_requests(
                        f"{self.model} temporarily unavailable (HTTP {response.status_code}); remaining work deferred. {message[:400]}",
                        retry_after or 300,
                        LLMServiceUnavailable,
                    )
                if response.status_code not in {429, 500, 502, 503, 504} or attempt + 1 == attempts:
                    raise RuntimeError(f"LLM HTTP {response.status_code}: {message[:600]}")
                time.sleep(min(60, max(retry_after, LLM_CONFIG["retry_delay"] * (2**attempt))))
                continue
            result = response.json()
            candidates = result.get("candidates") or []
            if not candidates:
                raise RuntimeError("LLM returned no candidate; input remains pending")
            candidate = candidates[0]
            finish = candidate.get("finishReason", "STOP")
            if finish == "MAX_TOKENS":
                raise LLMContextLimit("LLM output limit reached; retrying a smaller batch")
            if finish != "STOP":
                raise RuntimeError(f"LLM stopped with {finish}; input remains pending")
            content = "".join(
                str(part.get("text", "")) for part in candidate.get("content", {}).get("parts", [])
                if not part.get("thought")
            )
            if not content.strip():
                raise RuntimeError("LLM returned empty text; input remains pending")
            usage = result.get("usageMetadata", {})
            return {
                "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": usage.get("promptTokenCount", 0),
                    "completion_tokens": usage.get("candidatesTokenCount", 0),
                    "total_tokens": usage.get("totalTokenCount", 0),
                },
            }
        raise RuntimeError("LLM generation did not complete")
