"""Test için minimal MCP sunucusu (stdio, satır başına JSON-RPC)."""
import json
import sys

TOOLS = [
    {"name": "echo", "description": "metni geri döndür", "annotations": {"readOnlyHint": True},
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "fail", "description": "hata döndürür", "inputSchema": {"type": "object"}},
]

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m = msg["method"]
    if m == "initialize":
        res = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
               "serverInfo": {"name": "fake", "version": "1"}}
    elif m == "tools/list":
        if msg["params"].get("cursor") == "p2":
            res = {"tools": TOOLS[1:]}
        else:
            res = {"tools": TOOLS[:1], "nextCursor": "p2"}
    elif m == "tools/call":
        name = msg["params"]["name"]
        if name == "echo":
            res = {"content": [{"type": "text", "text": "echo: " + msg["params"]["arguments"]["text"]}]}
        else:
            res = {"content": [{"type": "text", "text": "olmadı"}], "isError": True}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "yok"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}), flush=True)
