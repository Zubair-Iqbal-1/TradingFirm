#!/usr/bin/env bash
# LIVE analyst check (Part 4.4, spec decision 14; also Part 4.8's script).
#
# *** THIS SPENDS REAL MONEY: one classifier call + one verdict call. ***
#
# Runs on the HOST against prod ai-agent's loopback port. The route is the
# live check, so no script has to exist inside any image (tests/ is not in
# the prod image). It reads no .env and never sees the key (G14).
#
#   ./scripts/analyze_live.sh AAPL            # one ticker first (G2)
#   ./scripts/analyze_live.sh AAPL 182.50     # with an entry
#
# Expected cost at OpenRouter's list price for anthropic/claude-sonnet-5
# ($2 / $10 per 1M tokens, read from its model page 2026-09-21), as measured
# on the first live call: about $0.058 for a ticker with a full batch of 30
# unlabelled headlines (classifier $0.039 + verdict $0.019), about $0.014 for
# a repeat inside the prompt cache's 5 minutes. Ceiling ~$0.13.
# The real figure is printed from OpenRouter's own usage.cost.
#
# Run only after a go in chat. Never from CI, never in a loop.
set -euo pipefail

TICKER="${1:?usage: analyze_live.sh TICKER [ENTRY]}"
ENTRY="${2:-}"
BASE="${AI_AGENT_URL:-http://127.0.0.1:8004}"

URL="$BASE/analyze/$TICKER?horizon=swing"
[ -n "$ENTRY" ] && URL="$URL&entry=$ENTRY"

echo "== usage before"
curl -sS -m 10 "$BASE/usage"; echo
echo "== POST $URL"
BODY="$(curl -sS -m 300 -X POST -w '\n%{http_code}' "$URL")"
echo "HTTP ${BODY##*$'\n'}"
printf '%s' "${BODY%$'\n'*}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
if "verdict" not in d:
    print(json.dumps(d, indent=2)); sys.exit(0)
v = d["verdict"]
print(json.dumps({k: d[k] for k in ("ticker", "cached", "stored", "verdictId", "entry",
      "entrySource", "regime", "macroStatus", "newsClassified", "classifier", "model", "usage")}, indent=2))
print("verdict:", v["verdict"], "confidence:", v["confidence"])
print("reasoning:", v["reasoning"])
for name in ("thesis", "thesisBreakers", "riskFlags"):
    print(name + ":"); [print("  -", x) for x in v[name]]
print("plan:", json.dumps(v["plan"], indent=2) if v["plan"] else d["planRejection"])
if d["usage"]:
    write = d["usage"].get("cacheWrite")
    print("cacheRead:", d["usage"].get("cacheRead"), "cacheWrite:", write, "->",
          "the prefix cleared the 1,024-token minimum" if write else
          "no cache write (flag off, or the prefix is under the minimum)")
'
echo "== usage after"
curl -sS -m 10 "$BASE/usage"; echo
