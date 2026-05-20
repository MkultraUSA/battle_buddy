# Hermes Model Routing Policy - Battle Buddy

## Objective
Use free models first with adaptive rate limiting, and only fall back to paid when free capacity/quality is insufficient.

## Routing Order
1. Use Hermes free-model recommendations feed:
   - `https://hermes.libertas.mobi/free-model-status/recommendations.json`
2. Select first model that is:
   - `status=online`
   - `supports_tools=true`
   - `score>=70`
3. If no qualifying free model is available, use paid fallback:
   - `deepseek-v4-pro`

## Bot Assignment
- `bb-codebot`:
  - primary: free recommendation route
  - fallback: `deepseek-v4-pro`
- `bb-reviewbot`:
  - primary: free recommendation route
  - fallback: `deepseek-v4-pro`
- `codex-second-pass`:
  - run only after `bb-reviewbot` approves.

## Guardrails
- Do not pin to a single free model permanently.
- Re-evaluate free model choice each run (or every 15 minutes with cache).
- Log selected model and whether fallback was used for each bot task.

## Throughput Controls
- Set global request mode to `low`.
- Keep at most one in-flight LLM request per bot.
- Insert 8-12 second jitter between LLM requests.
- For OpenAI-backed tasks, use the same low mode and backoff policy.
- If fallback is needed, use `deepseek-v4-pro` only after free-model route fails availability/quality checks.
- Add daily soft caps; pause new bot work when cap is reached and resume next window.
