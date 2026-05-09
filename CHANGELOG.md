# Changelog

All notable changes to this project will be documented in this file.

## [1.2.0] - 2026-05-09

### Added
- **`/image_proxy` 子命令体系**，支持在对话中动态管理模型链：
  - `primary <base> <key> <model> [timeout]` — 热切换主模型
  - `fallback add <base> <key> <model> <timeout>` — 添加备用模型
  - `fallback del <序号>` — 删除指定备用模型
  - `fallback clear` — 清空备用链
  - `fallback list` — 列出备用链
  - `stats reset` — 重置调用统计
  - `config` — 显示 AstrBot 配置片段
  - `help` / `帮助` — 显示帮助
- 所有变更**即时生效**，无需重启 AstrBot
- `ImageCaptionProxy` 热插拔 API：`update_primary`、`add_fallback`、`remove_fallback`、`clear_fallbacks`、`reset_stats`

### Changed
- `/image_proxy` 默认行为改为 status 子命令
- 内存热值标注：status 输出中标注当前配置来源

## [1.1.0] - 2026-05-09

### Changed
- **回退链重构**：从固定一主一备升级为 JSON 配置的任意级回退链
- **独立超时**：每个模型（含主模型）均可独立设置 timeout，不再共享全局超时
- 新增 `primary_timeout` 配置项，替代 `health_check_timeout`
- 新增 `fallback_chain`（JSON 数组）配置项，替代单一 fallback 字段
- 向后兼容旧配置格式（`fallback_api_base` / `fallback_api_key` / `fallback_model`）
- `/image_proxy` 命令显示完整回退链和每级超时信息

## [1.0.0] - 2026-05-09

### Added
- 初始版本发布
- 本地 HTTP 代理服务器，OpenAI 兼容 `/v1/chat/completions` 端点
- 主模型 → 备用模型自动回退路由
- `/image_proxy` 命令查看代理状态和配置指南
- 中英文 i18n 支持
- 零外部依赖，仅使用 Python 标准库
- 调用统计与命中率展示
