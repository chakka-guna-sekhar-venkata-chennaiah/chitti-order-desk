"""Proof: model ID spellings in GET /v1/models, and reasoning tokens inside completion_tokens with a short prompt.
Run from the repo root: .venv/bin/python -B evals/proofs/model_ids_and_short_prompt.py (2 paid calls). See FEEDBACK.md."""
import base64, json, time
from openai import OpenAI
from order_desk.config import load_settings
s = load_settings()
c = OpenAI(api_key=s.nebius_api_key, base_url=s.nebius_base_url, timeout=300, max_retries=0)
ids = sorted(m.id for m in c.models.list().data if m.id.lower().startswith("nvidia/"))
print("PROOF 1 - NVIDIA model IDs from GET /v1/models:")
for i in ids: print("  ", i)
img = base64.b64encode(open("evals/golden/chits/hard_01.png", "rb").read()).decode()
msg = [{"role": "user", "content": [
    {"type": "text", "text": 'Read this handwritten order list. Return only one JSON object: {"lines": [{"raw_text": "...", "quantity": 1}]}.'},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img}}]}]
print(f"PROOF 2 - same image (hard_01.png), same prompt, model {s.vision_model}, temperature 0:")
for mt in (4000, 16000):
    t = time.time()
    r = c.chat.completions.create(model=s.vision_model, messages=msg, temperature=0, max_tokens=mt)
    ch, u = r.choices[0], r.usage
    d = getattr(u, "completion_tokens_details", None)
    content = ch.message.content or ""
    reasoning = getattr(ch.message, "reasoning_content", None) or ""
    print("  ", json.dumps({"max_tokens": mt, "finish_reason": ch.finish_reason, "completion_tokens": u.completion_tokens,
        "reasoning_tokens": getattr(d, "reasoning_tokens", None) if d else None, "reasoning_chars": len(reasoning),
        "answer_chars": len(content), "answer_has_json": "{" in content, "seconds": round(time.time() - t, 1)}))
