# V2 阶段 5：容器形态。
#
# 关键选择：容器里跑**无头绑定**（remote / verify-ci / controlplane），
# 不跑 flet 桌面窗口——桌面窗口要求显示服务器（X11），容器里没有；
# 而 agent 的价值（写入路径、观察流、自证、跨机驱动）全部不依赖窗口。
# 这是"诚实降级"的另一处应用：不声明做不到的（window/snapshot），做到的说清楚。
#
#   构建并运行远程绑定：
#     docker build -t puppethub -f packaging/Dockerfile .
#     docker run -p 8765:8765 -v $(pwd)/demo-app:/app demo:latest remote /app
#
FROM python:3.12-slim

# 开发态从源码装；发布后改为 `pip install puppethub`（PyPI 是版本真源，
# 容器镜像只是"装好依赖的那台机器"的固化，不是另一条依赖解析路径）。
WORKDIR /src
COPY pyproject.toml README.md ./
COPY puppethub ./puppethub
COPY packaging ./packaging
RUN pip install --no-cache-dir .

EXPOSE 8765

# 默认跑远程绑定（无头多客户端）；verify-ci / repl / controlplane 换参数即可：
#   docker run ... puppethub verify-ci          # 自证以退出码说话
#   docker run ... puppethub repl /app          # 人作驱动者的手写程序模式
ENTRYPOINT ["puppethub"]
CMD ["remote", "/app", "--port", "8765"]
