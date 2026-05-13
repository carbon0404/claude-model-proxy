# Claude Desktop Model Proxy

Claude Desktop 强制校验模型名必须为标准 Claude 模型名（如 `claude-sonnet-4-6`），旧的加 `claude-` 前缀方案已失效。

本代理的核心能力：
1. **模型名映射** — 标准 Claude 名 → 任意提供商模型名
2. **多提供商路由** — 不同模型自动路由到不同 base URL + API key
3. **格式转换** — Anthropic Messages API ↔ OpenAI Chat Completions API（system 字段、content 结构、SSE 流式 chunk）
4. **Query 参数兼容** — 支持 `?beta=true` 等参数

## 工作原理

```
Claude Desktop                    proxy(:5679)                  提供商 API
─────────────────────────────────────────────────────────────────────────
POST /v1/messages?beta=true
  model: claude-sonnet-4-6
  system: "you are helpful"  ──→  ① 查路由表:
  messages: [...]                  sonnet → kimi-k2.6 @ moonshot
                                  ② 转换格式:
                                  system → role:system msg
                                  /v1/messages → /v1/chat/completions
                                  ③ 换 API key            ──→  POST /v1/chat/completions
                                  ④ 转发请求                     model: kimi-k2.6
                                                                  messages: [...]
                                  ⑤ 转换响应              ←──  200 OK (OpenAI 格式)
                                  OpenAI → Anthropic 格式
                                  ⑥ 恢复模型名
←── 200 (Anthropic 格式)
  model: claude-sonnet-4-6
```

## 映射表

| Claude Desktop 模型 | → | 实际模型 | Provider URL |
|---|---|---|---|
| `claude-opus-4-6` | → | `deepseek-v4-pro` | `api.deepseek.com/anthropic` |
| `claude-haiku-4-5-20251001` | → | `deepseek-v4-flash` | `api.deepseek.com/anthropic` |
| `claude-sonnet-4-6` | → | `kimi-k2.6` | `api.moonshot.cn/anthropic` |

## Shell 别名

将以下内容添加到 `~/.zshrc`（或 `~/.bashrc`）：

```bash
alias claude-desktop-proxy-start='nohup python3 ~/Documents/opensource/claude-model-proxy/proxy.py > ~/Documents/opensource/claude-model-proxy/proxy.log 2>&1 & echo "Proxy started (PID $!)"'
alias claude-desktop-proxy-stop='pkill -f "proxy.py" && echo "Proxy stopped"'
alias claude-desktop-proxy-status='pgrep -f "proxy.py" > /dev/null && echo "Proxy running (PID $(pgrep -f proxy.py))" || echo "Proxy not running"'
alias claude-desktop-proxy-log='tail -f ~/Documents/opensource/claude-model-proxy/proxy.log'
```

添加后执行 `source ~/.zshrc` 生效。

## 使用方式

```bash
# 启动
claude-desktop-proxy-start

# 查看状态
claude-desktop-proxy-status

# 查看实时日志
claude-desktop-proxy-log

# 停止
claude-desktop-proxy-stop

# 测试（mock server，不需要真实 API）
cd ~/Documents/opensource/claude-model-proxy
python3 runtest.py
```

## Claude Desktop 配置

```json
{
  "inferenceGatewayBaseUrl": "http://127.0.0.1:5679",
  "inferenceModels": [
    { "name": "claude-sonnet-4-6", "supports1m": true },
    { "name": "claude-opus-4-6", "supports1m": true },
    { "name": "claude-haiku-4-5-20251001", "supports1m": true }
  ]
}
```

关键是两个变更：
- `inferenceGatewayBaseUrl`：从原来的 `http://127.0.0.1:5678` 改为 `http://127.0.0.1:5679`
- `inferenceModels`：模型名改为标准 Claude 模型名，不再用 `claude-xxx` 前缀

重启 Claude Desktop 生效。

## 添加更多模型映射

编辑 `model_map.json`：

```json
{
  "claude-sonnet-4-6":          "kimi-k2.6",
  "claude-sonnet-4-5":          "kimi-k2.5",
  "claude-opus-4-6":            "deepseek-v4-pro",
  "claude-haiku-4-5-20251001":  "deepseek-v4-flash",
  "claude-haiku-3-5":           "qwen3.6-plus"
}
```

格式：`"标准Claude模型名": "提供商模型名"`，修改后重启代理生效。

提供商模型名必须在 `~/.claude-model-proxy/config.json` 的某个 provider 中存在（含对应的 `target_url` 和 `api_key`）。

### 可用模型（来自 `~/.claude-model-proxy/config.json`）

| 模型名 | Provider | URL |
|--------|----------|-----|
| `deepseek-v4-pro` | DeepSeek | `api.deepseek.com/anthropic` |
| `deepseek-v4-flash` | DeepSeek | `api.deepseek.com/anthropic` |
| `kimi-k2.5` | Moonshot | `api.moonshot.cn/anthropic` |
| `kimi-k2.6` | Moonshot | `api.moonshot.cn/anthropic` |
| `qwen3.6-plus` | 阿里 MaaS | `token-plan.cn-beijing.maas.aliyuncs.com` |
| `qwen3-coder-next` | 阿里 MaaS | `token-plan.cn-beijing.maas.aliyuncs.com` |
| `glm-5` | 阿里 MaaS | `token-plan.cn-beijing.maas.aliyuncs.com` |
| `MiniMax-M2.5` | 阿里 MaaS | `token-plan.cn-beijing.maas.aliyuncs.com` |

### 标准 Claude 模型名参考

- `claude-opus-4-6`
- `claude-sonnet-4-6`
- `claude-sonnet-4-5`
- `claude-haiku-4-5-20251001`
- `claude-haiku-3-5`
- `claude-opus-4-5`

只要是 Anthropic 官网上存在的模型名都能用。

## 日志格式说明

```
[05:55:18] >>> POST /v1/messages?beta=true              ← 收到请求
[05:55:18] POST /v1/messages?beta=true body={...}        ← 请求体预览
[05:55:18]   ↳ Anthropic→OpenAI  sonnet-4-6→kimi-k2.6   ← 模型映射+格式转换
  msgs=21 max_tok=32000 stream=True
[05:55:27]   ← provider 200 in 9313ms                    ← 提供商响应延迟
[05:55:32]   ✓ streaming done in 13888ms                 ← 流式完成总耗时
```

## 命令行选项

```bash
python3 proxy.py --port 5680                      # 自定义端口（默认 5679）
python3 proxy.py --mapping my_map.json            # 自定义映射文件
python3 proxy.py --providers ~/my_providers.json  # 自定义提供商配置
```

## 文件结构

```
claude-model-proxy/
├── proxy.py          # 代理主程序
├── model_map.json    # 模型名映射配置
├── runtest.py        # 集成测试（4 项，mock server）
├── start.sh          # 简易启动脚本
└── README.md         # 说明文档
```
