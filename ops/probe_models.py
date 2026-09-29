"""Which hosted models actually answer right now? Run: python ops/probe_models.py [model ...]

Being listed by /v1/models is not being callable: NVIDIA's free catalog lists
models that 404 ("Function not found for account") or queue indefinitely.
For each candidate this sends, in parallel, one plain request plus the two
JSON styles extract can use (response_format json_schema / json_object), each
with that model's thinking switch turned off where one exists. It prints
status, latency and whether usable JSON came back, and ends with the .env
lines for the best working model. The API key is read via ytbrain's .env
loader and never printed.
"""
from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from ytbrain import config  # noqa: F401  (loads .env)

# model -> request fields that switch its thinking off (None: no off switch / not needed)
THINKING_OFF: dict[str, dict | None] = {
    "nvidia/nemotron-3.5-lightning-30b-a3b": {"chat_template_kwargs": {"enable_thinking": False}},
    "nvidia/nemotron-3-super-120b-a12b": {"chat_template_kwargs": {"enable_thinking": False}},
    "nvidia/nemotron-nano-3-30b-a3b": {"chat_template_kwargs": {"enable_thinking": False}},
    "moonshotai/kimi-k2.6": {"chat_template_kwargs": {"thinking": False}},
    "google/gemma-4-31b-it": None,        # thinking is off by default
    "z-ai/glm-5.3-flash": None,           # GLM-5.3 cannot disable thinking
    "deepseek-ai/deepseek-v4.1-flash": None,
}
SCHEMA = {"type": "object", "properties": {"tip": {"type": "string"}}, "required": ["tip"]}
BASE = os.environ.get("YTBRAIN_LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
KEY = os.environ.get("YTBRAIN_LLM_API_KEY", "")
TIMEOUT = 75
MODES = ("plain", "response_format", "json_object")


def call(model: str, mode: str) -> tuple[bool, str]:
    body = {"model": model, "max_tokens": 300, "temperature": 0.1,
            "messages": [{"role": "user", "content": "Say hi" if mode == "plain" else
                          'Give one startup tip as JSON: {"tip": "..."}'}],
            **(THINKING_OFF.get(model) or {}),
            **json.loads(os.environ.get("YTBRAIN_LLM_EXTRA_BODY") or "{}")}   # same extras extract sends
    if mode == "response_format":
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "tip", "schema": SCHEMA, "strict": False}}
    elif mode == "json_object":
        body["response_format"] = {"type": "json_object"}
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}/chat/completions", json=body, timeout=TIMEOUT,
                       headers={"Authorization": f"Bearer {KEY}"})
    except httpx.TimeoutException:
        return False, f"TIMEOUT >{TIMEOUT}s"
    except httpx.HTTPError as e:
        return False, f"ERROR {type(e).__name__}"
    dt = time.time() - t0
    if r.status_code != 200:
        return False, f"HTTP {r.status_code} {dt:4.1f}s {r.text[:50]!r}"
    msg = r.json()["choices"][0]["message"]
    content = msg.get("content")
    thinking = " +thinking" if (msg.get("reasoning_content") or msg.get("reasoning")) else ""
    if not content:
        return False, f"200 {dt:4.1f}s EMPTY{thinking}"
    if mode == "plain":
        return True, f"200 {dt:4.1f}s{thinking}"
    try:
        ok = "tip" in json.loads(content)
    except json.JSONDecodeError:
        ok = False
    return ok, f"200 {dt:4.1f}s {'valid-json' if ok else 'BAD-json'}{thinking}"


def main() -> None:
    if not KEY:
        sys.exit("YTBRAIN_LLM_API_KEY is not set (.env or environment)")
    models = sys.argv[1:] or list(THINKING_OFF)
    print(f"probing {len(models)} model(s) at {BASE} (timeout {TIMEOUT}s each) ...\n", flush=True)
    print(f"{'model':40} " + " ".join(f"{m:26}" for m in MODES))
    winners = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {(m, md): ex.submit(call, m, md) for m in models for md in MODES}
        for m in models:
            res = {md: futs[(m, md)].result() for md in MODES}
            print(f"{m:40} " + " ".join(f"{res[md][1]:26.26}" for md in MODES), flush=True)
            for md in ("response_format", "json_object"):
                if res[md][0]:
                    secs = float(res[md][1].split()[1].rstrip("s"))
                    # prefer: no thinking (it eats the output budget and time),
                    # then schema-enforced JSON, then speed
                    winners.append(("+thinking" in res[md][1], md != "response_format", secs, m, md))
                    break
    if not winners:
        print("\nNo model returned valid JSON. The free tier may be congested -- retry in a while.")
        return
    thinks, _, secs, m, md = min(winners)
    print(f"\nBest working model: {m} ({md}, {secs:.1f}s{', still thinks' if thinks else ''}). .env lines:\n")
    print(f"export YTBRAIN_LLM_MODEL={m}")
    print(f"export YTBRAIN_LLM_JSON_MODE={md}")
    extra = json.loads(os.environ.get("YTBRAIN_LLM_EXTRA_BODY") or "null") or THINKING_OFF.get(m)
    print(f"export YTBRAIN_LLM_EXTRA_BODY='{json.dumps(extra)}'" if extra
          else "unset YTBRAIN_LLM_EXTRA_BODY   # (remove the line from .env if present)")


if __name__ == "__main__":
    main()
