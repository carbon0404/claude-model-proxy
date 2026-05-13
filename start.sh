#!/bin/bash
# Start the Claude Desktop model proxy
cd "$(dirname "$0")"
nohup python3 proxy.py > proxy.log 2>&1 &
echo "Proxy started (PID $!), logs: proxy.log"
