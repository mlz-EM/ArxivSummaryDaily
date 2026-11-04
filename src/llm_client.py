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
        self.preferred_model = self.model
        configured = LLM_CONFIG.get("fallback_models", [])
        if isinstance(configured, str):
            configured = [name.strip() for name in configured.split(",") if name.strip()]
        self.models = list(dict.fromkeys([self.model, *configured]))
        self.models_used = []
        self.fallback_events = []
        self._local_deferred = {}
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

    def _deferred_models(self):
        path = self._quota_path()
        if not path or not path.exists():
            return dict(self._local_deferred)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                return dict(self._local_deferred)
            if isinstance(state.get("models"), dict):
                return {**state["models"], **self._local_deferred}
            # Read the original one-model marker without losing its cooldown.
            if state.get("model"):
                return {state["model"]: state, **self._local_deferred}
        except (OSError, ValueError, TypeError):
            pass
        return dict(self._local_deferred)

    def _check_deferred(self):
        state = self._deferred_models().get(self.model)
        if not state:
            return
        try:
            reset_at = datetime.fromisoformat(state["retryAt"])
            run_id = os.getenv("GITHUB_RUN_ID", "")
            same_run = bool(run_id and state.get("runId") == run_id)
            if same_run or reset_at > datetime.now(timezone.utc):
                error_type = LLMServiceUnavailable if state.get("errorType") == "LLMServiceUnavailable" else LLMQuotaExhausted
                raise error_type(str(state["reason"]))
        except (ValueError, KeyError, TypeError):
            pass

    def _defer_requests(self, reason, retry_after, error_type=LLMQuotaExhausted):
        self.stop_reason = reason
        self._deferred_error = error_type
        reset_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, retry_after))
        self._local_deferred[self.model] = {
            "reason": reason, "retryAt": reset_at.isoformat(),
            "errorType": error_type.__name__, "runId": os.getenv("GITHUB_RUN_ID", ""),
        }
        path = self._quota_path()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            models = self._deferred_models()
            temporary = path.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps({"schemaVersion": 2, "models": models}), encoding="utf-8")
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
        failures = []
        last_error = None
        for index, model in enumerate(self.models):
            self.model = model
            self.api_url = f"{LLM_CONFIG['api_url']}/{model}:generateContent"
            self.stop_reason = ""
            try:
                self._check_deferred()
                result = self._request_completion(messages, temperature, max_tokens)
            except (LLMQuotaExhausted, LLMServiceUnavailable) as exc:
                failures.append(str(exc))
                last_error = exc
                if index + 1 < len(self.models):
                    event = {"model": model, "reason": str(exc), "nextModel": self.models[index + 1]}
                    if event not in self.fallback_events:
                        self.fallback_events.append(event)
                        print(f"::warning::{model} deferred; trying configured fallback {self.models[index + 1]}.")
                continue
            self.stop_reason = ""
            if model not in self.models_used:
                self.models_used.append(model)
            print(f"LLM request completed with {model}.")
            return result
        self.stop_reason = "All configured models are deferred. " + " | ".join(failures)
        error_type = type(last_error) if last_error else LLMServiceUnavailable
        raise error_type(self.stop_reason)

    def _request_completion(self, messages, temperature=None, max_tokens=None):
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
                    self._defer_requests(
                        f"{self.model} network request failed after {attempts} attempts; remaining work deferred.",
                        300, LLMServiceUnavailable,
                    )
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
