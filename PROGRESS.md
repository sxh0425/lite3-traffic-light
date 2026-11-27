# 开发进度

## 2026-11-27 迁移基线

- 两份备份的 15 个共有源码、脚本和 YAML 文件逐个比对 SHA-256，内容一致。两份 README 和 AGENTS 描述的是同一代代码，包含历史运动部署说明。感知备份缺 PROGRESS 和测试，保留路线日志；运动备份另有采集、诊断、模拟数据生成、视觉与路线测试及历史实验。备份的环境文档混有 RK3588 记录，Jetson 以本次实机结果为准。
- 从感知备份迁入共同的 `src/light_state.py`、`src/camera_reader.py`、包初始化及默认阈值配置。保留历史采集入口兼容性，视觉预览需要显式传入视频源。由运动备份迁入模拟生成器和视觉测试，生成器兼容 Python 3.8，测试改为内存生成，公开仓库无需附带测试图片。新增 `src/preview.py`，沿用原判别及标注。
- 旧项目还实现带标签的采集、D435i 彩色和深度对齐、手动框测距、云端单灯判断和框选、路线内同灯三次确认。保留在备份中的内容包括云端评估、RealSense 深度路径、运动接口和为特定灯具设置的 ROI。视觉迁移版本使用 CPU、支持输入文件和单帧识别接口。
- 历史自购灯试采每类 12 张。默认规则在手工框选下红、绿和全灭通过，黄灯 12 张均拒识。旧实验基于同一灯具放宽黄色 Hue 后的结果只作为问题记录，该参数未迁入。云端版本整图试验只有四张，历史 `qwen4-max` 为 4/4，延迟中位数约 2.23 秒。自动框选当时主要是模拟响应验证。
- 未完成全幅自动定位、通用多目标关联与时序融合、黄灯跨场景泛化和板端 FPS 验收。后续先独立建立比赛灯具的全图定位基线，再把候选目标转换为灯位并做逐目标时序融合。自购灯只用于独立验证。

## 感知主机只读实测

| 项目 | 结果 |
| --- | --- |
| 系统 | NVIDIA Jetson Xavier NX Developer Kit，Ubuntu 20.04.6 LTS，aarch64，内核 5.10.120-tegra，系统 Python 3.8.10 |
| GPU 软件 | L4T R35.4.1 对应 JetPack 5.1.2 系列，JetPack 元包未列出。CUDA Toolkit 11.4.19，TensorRT Python 8.5.2.2。PyTorch 2.1.0a0+41361538.nv23.06 识别 1 块 CUDA GPU |
| Python 视觉 | 实际 OpenCV 4.9.0，Qt5、FFmpeg、V4L2 和采集超时属性可用。NumPy 1.23.1，PyYAML 5.3.1，pytest 8.1.1。普通 SSH 会话 DISPLAY 为空。Jetson 上有 Xorg、xauth 和 xdpyinfo，MobaXterm 的 X11 窗口待联调 |
| 摄像头 | RealSense 对应六个 video 节点，librealsense 2.50 存在。系统 Python 未直接导入 pyrealsense2，旧 RGB-D worker 可另行评估。检查未打开采集流，未观察到常用 RTSP 端口监听，已有 HTTP 采集目录仅视为线索 |
| 资源与目录 | 内存 6.7 GiB、检查时约 4.2 GiB 可用。系统盘 117 GiB、约 34 GiB 可用。部署目标为现有项目目录 `~/yidong` |
| 视觉模型 | `ddyolo_xavier` 有 DDYOLO 和 YOLOv10s 的 PT、ONNX、TensorRT FP16 资产、包内 Ultralytics 源码及独立环境线索。数据集类别为 SH、SP、SC、OP、MB、HB、CS、CFO、BMFO，暂不复用作红绿灯检测。其推理接口和 TensorRT 部署方法可作为参考，模型未加载推理 |

JetPack 版本对应关系见 [NVIDIA Jetson Linux 35.4.1](https://developer.nvidia.com/embedded/jetson-linux-r3541)。

## 部署与验证

- 正式开发目录是本地上级工作文件夹中的 `workspace`，也是 Git 仓库根目录。
- 按用户要求将部署目标改为感知主机现有的 `~/yidong`。四个新增视觉文件合入现有项目，五个共有文件经哈希一致性检查后登记。旧 README 等未列入部署。部署记录和回滚备份保存在项目内的隐藏目录。
- 首次部署已获确认，并在实机新增 4 个视觉文件、校验 5 个同内容文件，删除 0 个。首次板端测试的 Python `-s` 参数跳过了用户目录安装的 NumPy，移除该参数后使用已有 NumPy 1.23.1/OpenCV 4.9，71 项全部通过。部署号为 `9a5aadfb8772408bb5bb4fef599a3fab`。
- Windows 79 项通过。Jetson 无窗口预览成功识别合成绿灯，下载并核对 `results/deployment-smoke-20261127-750cbc16/vision.csv`，输出 GREEN，相对分数 0.760。摄像头实时采集和 MobaXterm X11 显示待联调。
