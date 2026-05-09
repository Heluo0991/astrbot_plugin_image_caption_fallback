# 图文转述回退代理

为 AstrBot 的图文转述（Image Caption）提供**多级回退链**代理。主模型不可达时按优先级依次尝试备用模型，每级独立设置超时。

## 解决的问题

AstrBot 的 `default_image_caption_provider_id` 只支持配置**单个**识图模型。当你的本地视觉模型（如 LM Studio 的 qwen-vl）没有启动时，AstrBot 会直接报错而非尝试其他可用模型。

本插件启动一个本地 HTTP 代理，插入在 AstrBot 和识图模型之间，支持**任意多级回退**：

```
AstrBot → POST localhost:11435/v1/chat/completions
              │
              ▼
         [代理服务器]
              │
              ├── 尝试主模型 (timeout: 5s)
              │      └── 失败 → 
              │
              ├── 尝试备用1 (timeout: 60s)
              │      └── 失败 →
              │
              └── 尝试备用2 (timeout: 90s)
                     └── 成功 → 返回结果
```

每个模型的超时**独立可配**：本地模型设短超时快速放弃，云端 API 设长超时给足推理时间。

## 环境要求

| 依赖 | 版本要求 |
|------|----------|
| Python | >= 3.10 |
| AstrBot | >= 4.23.0 |
| 外部依赖 | 无（仅使用 Python 标准库） |

## 安装

**方式一**：在 AstrBot 插件市场搜索「图文转述回退代理」，点击安装。

**方式二**：插件界面右下角点击加号 → 从链接安装，输入：
```
https://github.com/Heluo0991/astrbot_plugin_image_caption_fallback
```

## 配置

### 1. 插件配置

在 AstrBot 管理面板 → 插件配置中设置：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `primary_api_base` | `http://localhost:1234/v1` | 主模型 API 地址 |
| `primary_api_key` | `lm-studio` | 主模型 API Key |
| `primary_model` | `qwen/qwen2.5-vl-7b` | 主模型名称 |
| `primary_timeout` | `5` | 主模型超时（秒），本地设短、云端设长 |
| `fallback_chain` | _(JSON 数组)_ | 备用模型链，每项含 api_base/key/model/timeout |
| `proxy_port` | `11435` | 代理监听端口 |
| `proxy_host` | `127.0.0.1` | 监听地址 |

`fallback_chain` 格式示例：

```json
[
  {"api_base": "https://api.siliconflow.cn/v1", "api_key": "sk-xxx", "model": "Qwen/Qwen3-VL-32B-Instruct", "timeout": 60},
  {"api_base": "https://api.deepseek.com/v1", "api_key": "sk-yyy", "model": "deepseek-v4-pro", "timeout": 90}
]
```

> **兼容旧配置**：如果 `fallback_chain` 为空，插件会自动从旧的 `fallback_api_base`/`fallback_api_key`/`fallback_model` 字段读取（向后兼容）。

### 2. AstrBot 主配置

插件启动后，需要手动在 AstrBot 配置中添加代理作为新的 provider。发送 `/image_proxy` 命令可查看完整的配置片段。

简单来说，在 `cmd_config.json` 中做三处修改：

**2a. 在 `provider_sources` 中添加：**
```json
{
    "provider": "image_proxy",
    "type": "openai_chat_completion",
    "provider_type": "chat_completion",
    "key": ["proxy"],
    "api_base": "http://127.0.0.1:11435/v1",
    "id": "image_proxy",
    "enable": true
}
```

**2b. 在 `provider` 中添加：**
```json
{
    "id": "image_proxy/vision",
    "provider_source_id": "image_proxy",
    "model": "vision",
    "modalities": ["text", "image"],
    "enable": true
}
```

**2c. 修改 `provider_settings`：**
```json
"default_image_caption_provider_id": "image_proxy/vision"
```

### 3. 重启生效

配置完成后，重启 AstrBot 或发送新消息使配置生效。

## 使用

安装配置后无需任何操作。当有人给 Bot 发图片时：

1. AstrBot 调用代理提供的识图接口
2. 代理尝试**主模型**（如本地 LM Studio）
3. 如果主模型不可达 → 自动回退到**备用模型**（如 SiliconFlow 云端）
4. 用户完全无感

发送 `/image_proxy` 可查看代理运行状态和当前配置。

## 典型场景

### 场景一：本地优先 + 单云端兜底

```
主模型:  lm_studio / qwen2.5-vl-7b   (本地，5s 超时)
备用1:   siliconflow / Qwen3-VL-32B  (云端，60s 超时)
```

日常用本地模型，LM Studio 没启动时自动切云端。

### 场景二：全云端多级回退

```
主模型:  deepseek / deepseek-v4-pro      (10s 超时，应对网络波动)
备用1:   siliconflow / Qwen3-VL-32B      (60s 超时)
备用2:   openai / gpt-4o                 (90s 超时)
```

所有模型都是云端 API，主模型用短超时快速判断可达性，备用依次给足推理时间。

### 场景三：免费优先 + 付费兜底

```
主模型:  siliconflow / Qwen2.5-VL-7B    (免费额度，60s 超时)
备用1:   deepseek / deepseek-v4-pro      (付费，90s 超时)
```

### 场景四：同厂商不同模型降级

```
主模型:  siliconflow / Qwen3-VL-32B     (强模型，90s 超时)
备用1:   siliconflow / Qwen2.5-VL-7B    (弱模型，60s 超时，同 key)
```

## 项目结构

```
astrbot_plugin_image_caption_fallback/
├── main.py                  # 插件入口，Star 生命周期 + /image_proxy 命令
├── proxy_server.py          # HTTP 代理核心，回退路由逻辑
├── metadata.yaml            # 插件元数据
├── _conf_schema.json        # 配置 Schema
├── CHANGELOG.md
├── README.md
├── .astrbot-plugin/
│   └── i18n/
│       ├── zh-CN.json       # 中文翻译
│       └── en-US.json       # 英文翻译
└── .github/
    └── workflows/
        └── ci.yml
```

## 常见问题

**Q: 代理启动失败，提示端口被占用？**

A: 修改 `proxy_port` 配置项，换一个端口（如 11436），同时更新 provider_source 的 `api_base` 指向新端口。

**Q: 如何确认回退是否生效？**

A: 查看 AstrBot 日志，搜索 `Image caption proxy` 或 `Provider xxx failed`。当看到 `Provider <主模型> failed` 后紧接着 `Image caption succeeded via <备用模型>` 时，说明回退已生效。

**Q: 这个代理对对话模型有影响吗？**

A: 没有。代理只处理 `default_image_caption_provider_id` 指向的识图请求，不影响对话模型的选择和回退。

## 许可

AGPL-3.0 License

<div align="center">

**如果这个插件对你有帮助，请给个 ⭐ Star 支持一下！**

</div>
