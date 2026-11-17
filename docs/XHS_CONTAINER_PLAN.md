# 小红书容器接入准备
记录：2026-11-06 +08:00。当前仅准备配置，未启动 Docker Desktop、未拉取或运行容器。

Windows v2.7.2 桥和内置浏览器下载/校验成功。本机 127.0.0.1:18060 健康检查 HTTP 200，无 token 的接口访问 HTTP 401。登录二维码请求 HTTP 500，原因是 Windows 安全防护拦截其 leakless.exe 辅助进程。主控已验证身份后停止该 Windows 桥，没有修改安全设置、没有扫码登录或发布。

备选是同项目官方 Linux 容器版本 v2.7.2，Docker Hub 元数据显示发布时间 2026-11-03，linux/amd64 下载大小 405378996 字节，固定摘要 sha256:397dfd5361b25d1379d56e0218cff7ad2e1ed271e342ab580dbe7c082614e2f2。镜像内已带配套浏览器。官方依据：https://github.com/xpzouying/xiaohongshu-mcp/blob/v2.7.2/docker/docker-compose.yml

具体配置为 config/xhs-compose.yml，仅映射本机端口 127.0.0.1:18060；只挂载本项目 var/xhs-container/data 保存会话、var/drafts 只读供配图；口令经环境注入，不写入 compose；不挂载个人浏览器资料或其他目录。

待用户确认此替代运行方式后：启动现有 Docker Desktop，拉取固定摘要镜像并启动本项目容器；验证健康与口令，再获取二维码由本人扫码。Docker 镜像和应用仍属第三方代码，不把容器隔离当作可信或安全保证。

容器内图片路径是 /app/images，实际发布前还必须实现并验证 Windows 本地图片到该目录的明确映射。用户尚未批准任何真实作品发布；保持发布开关关闭。
