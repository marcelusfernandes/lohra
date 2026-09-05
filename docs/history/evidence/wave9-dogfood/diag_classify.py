import sys
sys.path.insert(0, "/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/w9-int/backend")
from lohra.config.env_file import apply_env_file
apply_env_file("/Users/marcelusfernandes/.lohra/.env")

import openai
from lohra.providers.errors import classify_provider_error, _status_of, _error_field, _MESSAGE_FINGERPRINTS

client = openai.OpenAI(api_key=__import__("os").environ["OPENROUTER_API_KEY"], base_url="https://openrouter.ai/api/v1")
try:
    client.chat.completions.create(model="nonexistent-vendor/e8b-xyz", messages=[{"role": "user", "content": "hi"}])
    print("NO EXCEPTION RAISED")
except Exception as exc:
    print("class:", type(exc).__module__, type(exc).__name__)
    print("status_code attr:", getattr(exc, "status_code", "MISSING"))
    print("status attr:", getattr(exc, "status", "MISSING"))
    print("_status_of:", _status_of(exc))
    print("top-level code attr:", repr(getattr(exc, "code", "MISSING")))
    body = getattr(exc, "body", "MISSING")
    print("body:", repr(body))
    print("error_field code:", repr(_error_field(exc, "code")))
    print("error_field message:", repr(_error_field(exc, "message")))
    print("fingerprints table key present:", (type(exc).__module__, type(exc).__name__, _status_of(exc)) in _MESSAGE_FINGERPRINTS)
    print("classify_provider_error ->", classify_provider_error(exc))
