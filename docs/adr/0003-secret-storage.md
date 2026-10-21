# ADR-0003 凭证存储：先用 0600 配置文件，后续迁移 Keychain

- 状态：待办（已知偏差）
- 日期：2026-10-20

## 背景

《设计文档》4.3 要求：凭证不能明文保存在 SQLite 中，应存放在系统安全存储，数据库只记录引用。

## 现状

模型配置（含 API Key）保存在 `~/Library/Application Support/zooagent/config/settings.json`，文件权限 `0600`，不出现在 SQLite 中，接口返回时一律脱敏（只返回 `api_key_set`）。

## 后续

- macOS：`security` / Keychain Services（`keyring` 库或原生 `security` CLI）
- Windows：Credential Manager；Linux：Secret Service
- 数据库与配置文件只保留 `credential_ref`

在此之前：不要把工作空间目录放进任何同步/共享位置（iCloud Drive、网盘、Git）。
