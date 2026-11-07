#!/bin/bash
nohup python web_server.py > web.log 2>&1 &
sleep 1
echo "✅ 服务已启动: http://127.0.0.1:8080"