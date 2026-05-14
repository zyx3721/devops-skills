# DevOps Skills

一个面向 Hermes 平台的 DevOps 技能库，提供监控诊断、任务编排、事件驱动等自动化能力。

## 📦 技能模块

### 1. Prometheus 监控诊断 (`prometheus/`)

**版本**: 2.0.0  
**描述**: Prometheus 细颗粒度查询与诊断技能，支持原生 Prom + 夜莺 Categraf 指标。

**核心特性**:
- 🔍 智能指标画像探测（自动识别 node_exporter / Categraf 风格）
- 📊 基数风险评估（避免高基数查询导致 token 爆炸）
- 📈 Summary First 策略（默认返回统计摘要，按需获取原始数据）
- 🎯 PromQL 优化与趋势分析

**适用场景**:
- 监控排障与性能诊断
- 指标查询与趋势分析
- PromQL 优化建议

---

### 2. Kanban Orchestrator (`kanban-orchestrator/`)

**版本**: 3.0.0  
**描述**: 任务分解与编排剧本，用于协调多个专业 Worker 协作完成复杂任务。

**核心特性**:
- 🎯 任务分解与路由（Decompose, Don't Execute）
- 🔄 多 Worker 协作编排
- 📋 任务生命周期管理
- 🚫 反诱惑规则（Orchestrator 只路由，不执行）

**适用场景**:
- 多专家协作任务（研究 + 分析 + 编写）
- 长时间运行任务（需要持久化）
- 人工审核流程（Human-in-the-loop）
- 并行任务执行

---

### 3. Kanban Worker (`kanban-worker/`)

**版本**: 2.0.0  
**描述**: Kanban Worker 的陷阱指南与最佳实践，自动注入到每个 Worker 的系统提示中。

**核心特性**:
- 📝 工作空间处理（scratch / dir / worktree）
- 🔒 租户隔离机制
- 🤝 良好的任务交接格式
- 🔄 任务生命周期（Orient → Work → Heartbeat → Block/Complete）

**适用场景**:
- 作为 Kanban Worker 执行具体任务
- 任务状态更新与交接
- 阻塞问题上报

---

### 4. Webhook Subscriptions (`webhook-subscriptions/`)

**版本**: 1.1.0  
**描述**: 事件驱动的 Agent 运行机制，通过 Webhook 接收外部服务事件触发自动化任务。

**核心特性**:
- 🔗 动态 Webhook 订阅管理
- 🔐 HMAC 签名验证
- 🎯 事件过滤与路由
- 🚀 自动触发 Agent 运行

**适用场景**:
- GitHub/GitLab 事件响应（PR、Issue、Push）
- CI/CD 集成（构建完成、部署通知）
- 监控告警响应（Prometheus Alertmanager）
- IoT 传感器数据处理

---

## 🚀 快速开始

### 前置要求

- [Hermes](https://github.com/anthropics/hermes) 平台已安装
- Python 3.8+（部分技能需要）
- Prometheus 实例（使用 `prometheus` 技能时）

### 安装技能

```bash
# 克隆仓库
git clone https://github.com/Jerion2/devops-skills.git
cd devops-skills

# 将技能目录链接到 Hermes 技能路径
ln -s $(pwd)/prometheus ~/.hermes/skills/prometheus
ln -s $(pwd)/kanban-orchestrator ~/.hermes/skills/kanban-orchestrator
ln -s $(pwd)/kanban-worker ~/.hermes/skills/kanban-worker
ln -s $(pwd)/webhook-subscriptions ~/.hermes/skills/webhook-subscriptions
```

### 使用示例

#### 1. Prometheus 监控诊断

```bash
# 查询 CPU 使用率趋势
hermes run --skill prometheus "分析过去 1 小时的 CPU 使用率趋势"

# 诊断内存异常
hermes run --skill prometheus "为什么 node1 的内存使用率突然飙升？"
```

#### 2. Kanban 任务编排

```bash
# 启动 Orchestrator 编排任务
hermes run --profile orchestrator "分析项目性能瓶颈并生成优化报告"

# Worker 自动接收任务并执行
```

#### 3. Webhook 事件响应

```bash
# 启用 Webhook 平台
hermes gateway setup

# 创建 GitHub PR 事件订阅
hermes webhook create \
  --name "pr-review" \
  --url "https://your-domain.com/webhook" \
  --events "pull_request" \
  --prompt "Review the PR and provide feedback"
```

---

## 📖 技能详细文档

每个技能模块都包含详细的 `SKILL.md` 文档：

- [Prometheus 技能文档](./prometheus/SKILL.md)
- [Kanban Orchestrator 文档](./kanban-orchestrator/SKILL.md)
- [Kanban Worker 文档](./kanban-worker/SKILL.md)
- [Webhook Subscriptions 文档](./webhook-subscriptions/SKILL.md)

---

## 🛠️ 技能开发

### 目录结构

```
devops-skills/
├── prometheus/
│   ├── SKILL.md              # 技能定义与文档
│   ├── scripts/              # 辅助脚本
│   └── references/           # 参考资料
├── kanban-orchestrator/
│   └── SKILL.md
├── kanban-worker/
│   └── SKILL.md
├── webhook-subscriptions/
│   └── SKILL.md
└── README.md
```

### 技能元数据格式

每个技能的 `SKILL.md` 文件头部包含 YAML 元数据：

```yaml
---
name: skill-name
description: "技能描述"
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [tag1, tag2]
    related_skills: [related-skill]
---
```

---

## 🤝 贡献指南

欢迎贡献新的 DevOps 技能或改进现有技能！

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/new-skill`)
3. 提交更改 (`git commit -m 'feat: 添加新技能'`)
4. 推送到分支 (`git push origin feature/new-skill`)
5. 创建 Pull Request

### 提交规范

遵循 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

- `feat`: 新功能
- `fix`: Bug 修复
- `docs`: 文档更新
- `refactor`: 代码重构
- `test`: 测试相关
- `chore`: 构建/工具链更新

---

## 📄 许可证

本项目采用 [MIT License](LICENSE) 开源协议。

---

## 🔗 相关链接

- [Hermes 官方文档](https://github.com/anthropics/hermes)
- [Prometheus 官方文档](https://prometheus.io/docs/)
- [问题反馈](https://github.com/Jerion2/devops-skills/issues)

---

## 📧 联系方式

- 作者: Jerion
- 邮箱: 416685476@qq.com
- GitHub: [@Jerion2](https://github.com/Jerion2)

---

**⭐ 如果这个项目对您有帮助，请给个 Star 支持一下！**
