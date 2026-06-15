# free_gpu_watcher

SSH 轮询多台共享 GPU 服务器，有空闲卡时弹 macOS 通知。

## 快速开始

```bash
pip install -r requirements.txt

# 编辑机器列表
vim config.yaml

# 后台启动（每天来了跑一次）
./gpu-watch start

# 查看实时状态
./gpu-watch logs

# 今天不用了
./gpu-watch stop
```

其他子命令：`status`（是否在跑）、`once`（一次性查询当前状态）。

---

技术细节见 [AGENTS.md](AGENTS.md)。
