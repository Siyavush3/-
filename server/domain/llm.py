"""Необязательная локальная языковая модель (узел 4).

Контур изолирован, GPU нет (ARCHITECTURE.md §1), поэтому тренажёр работает без модели: генерация,
эталон и оценка — правила. Если в контуре поднят сервер с OpenAI-совместимым API (llama.cpp server,
Ollama, vLLM) и задан A112_LLM_URL, модель перефразирует текст карточки по комментарию преподавателя,
который правила не распознали. Ответ модели проходит проверку орфографии; эталон модель не меняет.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional

SYSTEM = ("Ты редактор учебных сценариев для диспетчеров службы 112 Москвы. Перепиши текст по замечанию "
          "преподавателя. Сохрани все факты: адрес, номера, что случилось. Пиши по-русски, грамотно, одной-двумя "
          "фразами, без пояснений. Ответь только новым текстом.")


class LocalLLM:
    def __init__(self, url: Optional[str] = None, model: Optional[str] = None, timeout: float = 30.0):
        self.url = (os.environ.get("A112_LLM_URL", "") if url is None else url).rstrip("/")
        self.model = model or os.environ.get("A112_LLM_MODEL", "local")
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.url)

    def chat(self, system: str, user: str, max_tokens: int = 300) -> Optional[str]:
        body = {"model": self.model, "temperature": 0.3, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(self.url + "/v1/chat/completions", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
            return (data["choices"][0]["message"]["content"] or "").strip() or None
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            return None  # модель недоступна или ответила не по формату — работаем правилами

    def rewrite(self, text: str, comment: str) -> Optional[str]:
        out = self.chat(SYSTEM, "Текст: %s\nЗамечание преподавателя: %s" % (text, comment))
        if not out:
            return None
        line = out.strip().splitlines()[0].strip().strip("\"«»")
        return line[:500] or None
