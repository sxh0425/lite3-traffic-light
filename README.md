# Lite3 红绿灯视觉

本项目的 OpenCV 基线对画面中预先配置的三个灯位逐帧计算亮度、灯色和相对背景差，输出 RED、YELLOW、GREEN 或 UNKNOWN。预览窗口显示固定灯位框、单帧状态、相对分数和 FPS。默认配置对应模拟图片的灯位。识别分数尚未做概率校准。项目状态和代码来源见 [PROGRESS.md](PROGRESS.md)。

`src.preview` 在本机运行，启动不需要 API Key。

## Windows 开发与离线测试

在 VS Code 打开正式工作目录。从已有 Python 环境选用能导入 OpenCV、NumPy、PyYAML 和 pytest 的解释器。部署脚本只用 Python 标准库和 Windows OpenSSH。

```powershell
Set-Location .\workspace  # 从本项目的上级工作目录运行
python -B -m pytest -q -p no:cacheprovider tests/test_camera_reader.py tests/test_light_state.py
python -B -m unittest discover -s tests -p test_deployment_tools.py -q
python -B -m scripts.generate_simulated_data
python -B -m src.preview --kind image --source data/simulated/green_00.png --no-window
```

视觉测试在内存生成 63 张模拟图，并用本地临时文件验证图像和视频的读写。自动化测试验证增量写入、冲突检测、备份和中断续传。

## 部署与取回结果

通过 Windows SSH 配置中的 `Lite3-Perception` 和既有跳板连接。首次先运行预览，审核目标、变更和摘要，再确认执行。SSH 登录密码由当前终端交互输入。若终端没有读取正确的 SSH 配置，对两条命令均附加 `--ssh-config "$env:USERPROFILE/.ssh/config"`。

```powershell
python deploy.py --dry-run --test
python deploy.py --apply <预览给出的SHA256摘要> --test
python fetch_results.py demo/video.csv demo/annotated.jpg
```

目标是感知主机已有的 `~/yidong`。脚本核验 SSH 主机密钥、Xavier NX 硬件和路径。首次部署时，与本地相同的现有文件只登记哈希。目标路径已有同名异内容文件，或已登记的文件后来被修改或移除时，脚本停止并报告冲突。可选的 `--test` 只执行上述两份离线视觉测试，预览和 apply 需使用相同选项。

只有显式允许的文件新增或变化时才从 Windows 发送内容。脚本在项目内的 `~/yidong/.yidong-vision-deploy` 暂存代码、做完整性检查和可选测试，然后逐个提交到原路径并再次校验。修改前的版本保存在本次部署号下的 `before` 目录。部署过程一旦中断，使用原摘要重试。实验结果保存到 `~/yidong/results`。

下载时传入相对于远端 `results` 的确切文件名，支持 CSV、JSON、文本报告以及选定的图片和视频。脚本校验 SHA-256，写到 Windows 的 `results`，同名文件会停止以免覆盖。

## MobaXterm 只运行视觉

获准部署后，在 MobaXterm 打开 X server，用已配置的 SSH 跳板连接感知主机并启用 X11 转发。首次用合成图片检查窗口。关闭窗口可按 `q` 或 Esc。

```bash
cd ~/yidong
hostname
printf 'DISPLAY=%s\n' "$DISPLAY"
xdpyinfo >/dev/null
/usr/bin/python3 -B -m scripts.generate_simulated_data --output "$HOME/yidong/results/simulated"
/usr/bin/python3 -B -m src.preview --kind image \
  --source "$HOME/yidong/results/simulated/green_00.png"
```

本地视频示例。先把要用的片段放到感知主机项目的结果目录或指定其他已有可读路径。

```bash
/usr/bin/python3 -B -m src.preview --kind video --source "$HOME/yidong/results/demo/clip.mp4" \
  --csv "$HOME/yidong/results/demo/video.csv"
```

现场确认 RGB 设备节点且空闲后，调用 USB 预览。设备节点需按现场枚举结果填写。已有可用 RTSP 地址时，可改用 `--kind rtsp --source <地址>`。X11 转发不可用时加 `--no-window`，仍可输出 CSV。USB 的底层 V4L2 读帧可能阻塞，外层 `timeout` 限制整个运行。

```bash
timeout 45s /usr/bin/python3 -B -m src.preview --kind usb --source "$RGB_VIDEO_NODE" --seconds 30
```
