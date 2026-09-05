import sys
sys.path.insert(0, "/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/w9-int/backend")
from lohra.config.env_file import apply_env_file
apply_env_file("/Users/marcelusfernandes/.lohra/.env")

import openai, os
from lohra.providers.errors import classify_provider_error, _status_of, _error_payload, _error_code_of, _MESSAGE_FINGERPRINTS

client = openai.OpenAI(api_key=os.environ["OPENROUTER_API_KEY"], base_url="https://openrouter.ai/api/v1")
try:
    client.chat.completions.create(model="nonexistent-vendor/e8c-xyz", messages=[{"role": "user", "content": "hi"}])
    print("NO EXCEPTION RAISED")
except Exception as exc:
    print("class:", type(exc).__module__, type(exc).__name__)
    print("status_code attr:", getattr(exc, "status_code", "MISSING"))
    print("_status_of:", _status_of(exc))
    print("body attr:", repr(getattr(exc, "body", "MISSING")))
    print("_error_payload:", _error_payload(exc))
    print("_error_code_of:", _error_code_of(exc))
    status = _status_of(exc)
    print("fingerprints table key present:", (type(exc).__module__, type(exc).__name__, status) in _MESSAGE_FINGERPRINTS)
    print("classify_provider_error ->", classify_provider_error(exc))
