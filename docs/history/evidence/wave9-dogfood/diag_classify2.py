import sys
sys.path.insert(0, "/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/w9-int/backend")
from lohra.config.env_file import apply_env_file
apply_env_file("/Users/marcelusfernandes/.lohra/.env")

import openai, os

client = openai.OpenAI(api_key=os.environ["OPENROUTER_API_KEY"], base_url="https://openrouter.ai/api/v1")
try:
    client.chat.completions.create(model="nonexistent-vendor/e8b-xyz", messages=[{"role": "user", "content": "hi"}])
except Exception as exc:
    print("str(exc):", str(exc))
    resp = getattr(exc, "response", None)
    print("response type:", type(resp))
    if resp is not None:
        print("raw text:", getattr(resp, "text", "MISSING"))
    print("body attr:", repr(getattr(exc, "body", "MISSING")))
