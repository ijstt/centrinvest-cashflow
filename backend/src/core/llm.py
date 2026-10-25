import json
import logging
import urllib.request
import urllib.error
from src.config import settings

logger = logging.getLogger("llm_engine")

MODELS_FALLBACK_CHAIN = [
    "gemini-2.5-flash",
    "gemini-1.5-flash",
]

class ResilientLLMClient:
    def __init__(self, api_keys: list[str]):
        self.api_keys = api_keys if api_keys else [""]
        self.key_index = 0

    def _get_next_key(self) -> str:
        key = self.api_keys[self.key_index % len(self.api_keys)]
        self.key_index += 1
        return key

    def generate(self, prompt: str, system_prompt: str = "") -> str:
        contents = []
        if system_prompt:
            contents.append({"role": "user", "parts": [{"text": f"System Directive: {system_prompt}"}]})
            contents.append({"role": "model", "parts": [{"text": "Understood and acknowledged."}]})
        
        contents.append({"role": "user", "parts": [{"text": prompt}]})
        
        payload = {
            "contents": contents,
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 2048
            }
        }
        json_data = json.dumps(payload).encode("utf-8")

        # Try models in chain with bounded timeout
        for model in MODELS_FALLBACK_CHAIN:
            key = self._get_next_key()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            req = urllib.request.Request(
                url,
                data=json_data,
                headers={"Content-Type": "application/json"}
            )
            try:
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    return text
            except Exception as e:
                logger.warning("Model %s failed (%s)", model, type(e).__name__)
                continue
        raise RuntimeError("All LLM providers unavailable")

llm_client = ResilientLLMClient(settings.api_keys_list)
