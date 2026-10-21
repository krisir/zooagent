# ADR-0001 平台基线

- 状态：确定（沿用《设计文档 V1.0》）
- 日期：2026-10-20

## 决策

- 桌面容器：Tauri 2（macOS 首发，Windows/Linux 可复用）
- 前端：React + TypeScript + Vite
- 后端：Python + FastAPI，作为 sidecar 打包
- 存储：SQLite，本地优先；每个 Application 独立 `data.db`，系统数据在 `platform.db`
- 业务模型：元数据驱动（固定物理表 `records` + `data_json`），不为每个模型建表
- 界面：声明式 UI Schema，由固定组件渲染，不生成代码

## 影响

- 打包必须包含 Python 运行时（PyInstaller onefile，约 18 MB）。
- 业务内核不依赖 macOS 路径；工作空间目录按平台解析（`ZOOAGENT_HOME` 可覆盖）。
- 每应用独立数据库带来的取舍：跨应用事务与全局搜索更复杂（首版不需要）。
