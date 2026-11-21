#!/bin/sh
# A stand-in for Claude Code: answers --version and update, and in -p mode
# writes a file and prints stream-json like the real program.
case "$1" in
  --version) echo "2.1.0 (Claude Code)"; exit 0 ;;
  update) echo "Claude Code is up to date"; exit 0 ;;
esac
if [ -n "$FAKE_CLAUDE_FAIL" ]; then
  echo '{"type":"result","subtype":"error_max_turns","is_error":true,"result":""}'
  exit 1
fi
case " $* " in
  *" --disallowedTools "*)
    # A plan: read-only, unless the test asks it to misbehave.
    if [ -n "$FAKE_CLAUDE_PLAN_WRITES" ]; then printf 'oops\n' > oops.txt; fi
    printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"result":"## Plan\n1. Add hello.txt\n2. Check it with cat"}'
    exit 0 ;;
esac
printf 'hello from claude\n' >> hello.txt
cat <<'JSON'
{"type":"system","subtype":"init","model":"claude-sonnet-4-5","claude_code_version":"2.1.0"}
not json, ignored
{"type":"assistant","message":{"model":"claude-sonnet-4-5","content":[{"type":"text","text":"I'll add the file."},{"type":"tool_use","name":"Write","input":{"file_path":"hello.txt","content":"hello"}}]}}
{"type":"result","subtype":"success","is_error":false,"result":"Added hello.txt.","duration_ms":4200,"num_turns":2,"modelUsage":{"claude-sonnet-4-5":{"inputTokens":100,"outputTokens":50,"cacheReadInputTokens":1000,"cacheCreationInputTokens":200}}}
JSON
