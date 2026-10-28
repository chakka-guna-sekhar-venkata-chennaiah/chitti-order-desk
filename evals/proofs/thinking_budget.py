"""Proof: reasoning tokens count against max_tokens on nvidia/Nemotron-3.5-Super-VL.
Run from the repo root: .venv/bin/python -B evals/proofs/thinking_budget.py (2-3 paid calls). See FEEDBACK.md."""
import json
from order_desk.config import load_settings
from order_desk.vision.reader import TokenFactoryReader, prepare_image
s = load_settings()
img, mime = prepare_image(open("evals/golden/chits/hard_01.png", "rb").read())
print(f"PROOF 3 - the app's real reader prompt, hard_01.png, {s.vision_model}:")
for mt in (4000, 16000):
    r = TokenFactoryReader(s.nebius_api_key, s.nebius_base_url, s.vision_model, timeout_s=300, max_tokens=mt)
    calls, orig = [], r._client.chat.completions.create
    def spy(*a, **k):
        resp = orig(*a, **k); calls.append(resp); return resp
    r._client.chat.completions.create = spy
    try:
        reading = r.read_chit_image(img, mime); result = f"OK, {len(reading.lines)} lines"
    except Exception as e:
        result = "ERROR: " + str(e)[:90]
    for i, resp in enumerate(calls, 1):
        u = resp.usage; d = getattr(u, "completion_tokens_details", None)
        print("  ", json.dumps({"max_tokens": mt, "call": i, "finish_reason": resp.choices[0].finish_reason,
              "completion_tokens": u.completion_tokens, "reasoning_tokens": getattr(d, "reasoning_tokens", None) if d else None,
              "answer_chars": len(resp.choices[0].message.content or "")}))
    print("   result:", result)
