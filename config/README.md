<!--
Author: wilbur
Version: 1.0
Date: 2026-09-08
Description: 说明本目录仅保留模板与说明文件；运行时配置在 ~/.flamingo/config/。
-->

# config/ —— 模板与说明

本目录**不是**运行时配置目录。Flamingo 读写的是 `~/.flamingo/config/`。

| 本目录文件 | 用途 |
|---|---|
| `models.example.yaml` | 模型配置模板。把需要的 provider/key 抄到 `~/.flamingo/config/models.yaml`（或用 Web 设置页配置后保存）。**不会**自动拷贝。 |
| `tools.yaml` | 程序默认工具配置模板。新机器首次运行自动拷贝到 `~/.flamingo/config/tools.yaml`；已存在不覆盖。 |
| `systemPrompt.md` | 程序默认系统提示词模板。同上，自动拷贝且不覆盖。 |

运行时技能目录是 `~/.flamingo/config/skills/`（每个技能一个文件夹 + 大写 `SKILL.md`），本仓库不再存放私人技能。

## 家目录布局

```
~/.flamingo/
  config/            # 运行时配置
    models.yaml      # 用户私有（含密钥）；缺失则报「无可用模型」
    tools.yaml
    systemPrompt.md
    skills/
  logs/              # 会话日志与用量
  auth.json          # OAuth 凭据（0700/0600）
```

## 重置

删除 `~/.flamingo/config/` 下对应文件后重跑 CLI 或 Web：`tools.yaml` / `systemPrompt.md` / `skills/` 会重新初始化。`models.yaml` **不会**自动生成，需参照本目录 `models.example.yaml` 手动配置（或 Web 设置页保存）。未配置就发请求会报「无可用模型」，属预期。
