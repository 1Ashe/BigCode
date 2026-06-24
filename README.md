# BigCode

一个参考 Claude Code 的 Coding Agent，由于 nano、mimo、small 都被用了，于是就取名叫 Big。

## 环境准备

```bash
conda create -n bigcode python=3.12
conda activate bigcode
pip install -e .
```

## 配置

### 1. 设置 API Key 环境变量

```bash
export DEEPSEEK_API_KEY="sk-..."
```

### 2. 配置模型 `~/.bigcode/models.json`

```json
{
  "default_model": "deepseek:deepseek-v4-pro",
  "providers": {
    "deepseek": {
      "base_url": "https://api.deepseek.com/anthropic",
      "api_key_env": "DEEPSEEK_API_KEY",
      "models": {
        "deepseek-v4-pro": {
          "id": "deepseek-v4-pro",
          "context_window": 1000000
        }
      }
    }
  }
}
```

- `default_model` — 默认使用的模型，格式为 `provider名:模型key`
- `providers.<name>.base_url` — API 地址
- `providers.<name>.api_key_env` — API Key 对应的环境变量名
- `providers.<name>.models` — 该 provider 下的模型列表

OpenAI 兼容协议将 `protocol` 设为 `"openai"` 即可。

## 运行

```bash
bigcode repl
```

输入 `/help` 查看可用命令。
