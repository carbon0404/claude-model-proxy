# Claude Desktop Model Proxy

让 Claude Desktop 使用任意第三方模型的本地代理。Claude Desktop 强制校验模型名必须为标准 Claude 模型名（如 `claude-sonnet-4-6`），本代理将其映射到 DeepSeek、Moonshot、阿里百炼等提供商的模型上。

核心能力：
- **模型名映射** — 标准 Claude 名 → 任意提供商模型名
- **多提供商路由** — 不同模型自动路由到不同 base URL + API key
- **格式转换** — Anthropic Messages API ↔ OpenAI Chat Completions API
- **流式支持** — SSE chunk 级别实时转换

## 快速开始

### 1. 配置模型提供商

```
cp provider-config.template.json ~/.claude-model-proxy/config.json
```

编辑 `~/.claude-model-proxy/config.json`，填入你的 API Key 并增删需要的提供商：

```json
{
  "providers": [
    {
      "target_url": "https://api.deepseek.com/anthropic",
      "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "models": [
        { "name": "deepseek-v4-pro", "to_1m": "auto" },
        { "name": "deepseek-v4-flash", "to_1m": "auto" }
      ]
    },
    {
      "target_url": "https://api.moonshot.cn/anthropic",
      "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "models": [
        { "name": "kimi-k2.5", "to_1m": "auto" },
        { "name": "kimi-k2.6", "to_1m": "auto" }
      ]
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `target_url` | 提供商的 API 地址（需兼容 OpenAI 格式） |
| `api_key` | 你的 API Key |
| `models[].name` | 该提供商提供的模型名，后续路由表将引用此名称 |
| `thinking_effort` | 可留空 |

模板 `provider-config.template.json` 中预置了 DeepSeek、Moonshot、智谱、阿里百炼等常用提供商的地址骨架。

### 2. 配置模型路由表

编辑项目目录下的 `model_map.json`，将 Claude 标准模型名映射到提供商模型名：

```json
{
  "claude-opus-4-6":            "deepseek-v4-pro",
  "claude-sonnet-4-6":          "kimi-k2.6",
  "claude-haiku-4-5-20251001":  "deepseek-v4-flash"
}
```

格式：`"标准Claude模型名": "提供商模型名"`。右侧的模型名必须在 `~/.claude-model-proxy/config.json` 的某个 provider 中存在。

可用的 Claude 标准模型名：`claude-opus-4-6`、`claude-sonnet-4-6`、`claude-sonnet-4-5`、`claude-haiku-4-5-20251001`、`claude-haiku-3-5`、`claude-opus-4-5` 等。

### 3. 运行测试

```bash
cd ~/Documents/opensource/claude-model-proxy
python3 runtest.py
```

测试使用 mock server 模拟提供商，不消耗真实 API 额度，验证路由和格式转换是否正常。输出全部 PASS 即配置正确。

### 4. 启动代理

```bash
# 前台运行（调试用）
python3 proxy.py

# 后台运行
nohup python3 proxy.py > proxy.log 2>&1 &
```

启动后输出路由表和监听端口（默认 5679）。

### 5. 配置 Claude Desktop

在 Claude Desktop 设置中添加：

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

重启 Claude Desktop 后，选择列表中的模型即可路由到对应提供商。

> 注意：`inferenceModels` 中的模型名需与 `model_map.json` 中配置的 Claude 名一致。

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

## Shell 别名（可选）

添加到 `~/.zshrc` 或 `~/.bashrc`：

```bash
alias claude-desktop-proxy-start='nohup python3 ~/Documents/opensource/claude-model-proxy/proxy.py > ~/Documents/opensource/claude-model-proxy/proxy.log 2>&1 & echo "Proxy started (PID $!)"'
alias claude-desktop-proxy-stop='pkill -f "proxy.py" && echo "Proxy stopped"'
alias claude-desktop-proxy-status='pgrep -f "proxy.py" > /dev/null && echo "Proxy running (PID $(pgrep -f proxy.py))" || echo "Proxy not running"'
alias claude-desktop-proxy-log='tail -f ~/Documents/opensource/claude-model-proxy/proxy.log'
```

执行 `source ~/.zshrc` 生效。

```bash
claude-desktop-proxy-start    # 启动
claude-desktop-proxy-status   # 查看状态
claude-desktop-proxy-log      # 实时日志
claude-desktop-proxy-stop     # 停止
```

## 命令行选项

```bash
python3 proxy.py --port 5680                      # 自定义端口（默认 5679）
python3 proxy.py --mapping my_map.json            # 自定义映射文件
python3 proxy.py --providers ~/my_providers.json  # 自定义提供商配置
```

## 日志格式

```
[05:55:18] >>> POST /v1/messages?beta=true              ← 收到请求
[05:55:18] POST /v1/messages?beta=true body={...}        ← 请求体预览
[05:55:18]   ↳ Anthropic→OpenAI  sonnet-4-6→kimi-k2.6   ← 模型映射+格式转换
  msgs=21 max_tok=32000 stream=True
[05:55:27]   ← provider 200 in 9313ms                    ← 提供商响应延迟
[05:55:32]   ✓ streaming done in 13888ms                 ← 流式完成总耗时
```

## 文件结构

```
claude-model-proxy/
├── proxy.py                      # 代理主程序
├── model_map.json                # 模型名映射配置
├── provider-config.template.json # 提供商配置模板（复制到 ~/.claude-model-proxy/ 后编辑）
├── runtest.py                    # 集成测试（mock server，不消耗额度）
├── start.sh                      # 简易启动脚本
├── .gitignore
└── README.md
```

## 常见问题

**Q: 启动报 `Provider config not found`？**

未配置提供商信息。执行 `cp provider-config.template.json ~/.claude-model-proxy/config.json` 并填入真实 API Key。

**Q: 返回 502 "Model not configured"？**

`model_map.json` 中的模型名未在 provider config 中找到匹配。检查两侧的模型名是否一致。

**Q: 如何添加新模型？**

先在 `~/.claude-model-proxy/config.json` 的某个 provider 的 `models` 数组中添加模型名，再在 `model_map.json` 中建立 Claude 名到该模型名的映射，重启代理即可。
