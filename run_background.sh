#!/bin/bash
cd /Users/apex/Documents/Codex/2026-11-14/mcp
lsof -ti :8080 | xargs kill -9 2>/dev/null
pkill -f ngrok 2>/dev/null
nohup /usr/local/bin/ollama serve > /dev/null 2>&1 &
sleep 1
nohup /usr/bin/python3 /Users/apex/Documents/Codex/2026-11-14/mcp/server.py > server.log 2>&1 &
NGROK_BIN=$(command -v ngrok || echo "/Users/apex/Documents/Codex/2026-11-14/mcp/ngrok")
nohup "$NGROK_BIN" http --url=bucket-providing-suds.ngrok-free.dev 8080 > ngrok.log 2>&1 &
sleep 2
open "https://ngrok-free.dev"
