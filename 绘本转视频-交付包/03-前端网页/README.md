# 纸书智伴 · 绘本阅读工作台

本目录是当前唯一主工程：FastAPI 后端及无需构建的 HTML/CSS/JavaScript 前端。最终目标是支持未针对其定制的授权绘本完整导入、理解、生成与交互；当前仍需真实服务和跨书整体验证。

## 安装与启动

建议 Python 3.12。在本目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python run.py
```

Windows 可使用 `安装.cmd`、`启动.cmd` 或已有的一键启动脚本；Python 命令可替换为 `.venv\Scripts\python.exe`。默认仅在本机 <http://127.0.0.1:8765> 提供服务。

`requirements.lock.txt` 固定本次验证的 Python 包版本；`requirements.txt` 声明直接依赖范围。依赖更新后需重新运行验证，不能沿用旧结果。Playwright 浏览器另行安装，离线单元测试不需要浏览器。

媒体检查需要 FFmpeg 和 ffprobe。macOS 可执行 `brew install ffmpeg`，Windows 可执行 `python tools/install_ffmpeg.py`。也可将可信的对应平台二进制放入 `tools/bin/`。已有的 imageio-ffmpeg 提供 FFmpeg 备用程序，但不含 ffprobe；完整录屏帧数验证仍需要真正的 ffprobe。

## 配置模型

复制 `config.local.example.json` 为 `config.local.json`，按本机已有服务修改地址和模型 ID。默认组合沿用交付包的视觉/文本模型、百炼 ASR/TTS、MiniMax 视频；模板中的模型能否调用需以账号权限和实际接口为准。

模板的 `api_key` 是 `env:BOOK_VISION_API_KEY`、`env:BOOK_CHAT_API_KEY`、`env:BOOK_ASR_API_KEY`、`env:BOOK_TTS_API_KEY`、`env:BOOK_VIDEO_API_KEY`。在本机安全设置这些变量后启动程序；不要将密钥写入聊天、源码或提交文件。也可通过 `BOOK_CONFIG_FILE` 指向其他私有配置文件。程序不会自动加载 `.env`。

可选的 `interaction_vision`（环境变量示例 `BOOK_INTERACTION_VISION_API_KEY`）只用于点读、画面问答和视觉复核，不改变 `vision` 负责的导入/OCR 链路。可在“服务配置 → 理解 → 互动看图”单独填写；服务地址和模型均留空时沿用读图服务，密钥留空则保留已保存值。只填部分必需项会显示未配置，不会偷偷切到其他服务。`/api/status` 的 `interaction_vision.source` 标明实际选用的配置角色。模型是否可用仍以账号权限与真实调用为准。

`config.local.json`、运行数据和密钥被 Git 忽略。配置值不会因有模板而自动变为已可用：真实联调需有实际服务权限。

MiniMax 视频服务的开通入口与本机填写步骤见 [视频服务配置指引](docs/minimax-setup.md)。

数据发送范围：视觉模型接收页面及部分交互视频帧；文本模型接收提取后的绘本文字、画面描述、问题和会话上下文；ASR 接收提问音频；TTS 接收旁白或回答；视频服务接收参考页图和动画提示。问答还会额外调用视觉或文本模型核对证据。请仅使用授权允许的服务和素材。

点击识别只向互动视觉服务发送实际视频整帧及同尺寸点击标记副本，输出必须使用 `full_frame` 的 0–1000 整数角点；不再同时发送不同坐标系的裁剪图或原书对象名称，避免抄用原图/邻近对象。候选框包含点击点后，再以原帧、标记图和未标记的点击像素细节独立复核；空白或未确认目标不展示对象框。对象动作追问仍可使用同一冻结帧的局部参考；红色标记只用于定位。点击名称不充当故事证据，也不据外观猜姓名。视频位置变化后需重新点选。

用户于 2026-11-22 转述的赛事补充要求已纳入开发验收。打开交互视频后，可提问 `查找“目标文字”` 或 `高亮“目标文字”`，也可通过语音转写提出同类指令。系统核对实际视频帧，定位成功后暂停并分别显示轮廓框或黄色高亮；原书文字仅排序候选片段，不作为视频中已有文字的证明。当前采用有上限的采样，未命中只表示本次未确认。点击物体会在其独立窗口自动补充有原书证据的相关背景；身份或关联无法确认时明确提示。详细使用、数据字段及验证限制见 [新增交互要求记录](docs/video-text-requirements-2026-11-22.md)。无需新增服务配置，沿用已有互动视觉与文本服务。

直接提问 `大象在哪里`、`找到小兔子` 会查找实际视频物体并暂停框选；`查找“大象”` 或 `高亮“大象”` 查找的是文字。位置命中也经过独立像素复核，原书内容不能证明视频中存在物体；`故事里大象在哪里` 则仍走有原书证据的问答。详见 [真实识别修补记录](docs/recognition-repair-2026-11-22.md)。

对象小助手可拖动标题栏移动，支持鼠标/触屏，也可聚焦标题栏后按方向键移动。窗口会限制在可见区域；短屏输入区可滚动。背景临时失败后可点“重新获取背景”，继续使用同一对象；不自动重试未知提交。背景仅把对象外观作为消歧线索，事实仍须引用原书并复核；新版上下文不会沿用旧版背景缓存。验证、时延和边界见 [对象背景与移动窗口记录](docs/object-background-window-2026-11-22.md)。

## 流程和数据

| 目录/模块 | 职责 |
|---|---|
| `storybook/pipeline.py` | PDF 原图/文字提取、视觉理解、故事、旁白、视频及采样质量检查 |
| `storybook/interaction.py`、`answer_validation.py` | 检索、严格草稿校验、独立证据复核、播报、实际帧触控 |
| `storybook/followup.py`、`grounding.py` | 当前帧/原书背景分开生成与复核、点击标记、严格坐标转换 |
| `storybook/video_text.py`、`video_objects.py`、`web/video-annotations.js` | 实际视频文字/物体核对、可定位帧记录、暂停框选/高亮及标注清理 |
| `storybook/object_agents.py`、`web/object-agent.js` | 独立对象会话、自动原书背景、证据及同对象复用 |
| `storybook/fidelity.py` | 显式诊断用原图细节保护试验；未接入默认生成或发布 |
| `storybook/recovery.py` | 指纹校验后复用既有媒体，合成带配音的新书并披露替代动效 |
| `storybook/providers.py` | 外部模型协议、超时、服务错误及视频任务 ID 记录 |
| `storybook/server.py`、`storage.py` | HTTP 接口、任务状态、按书存储、历史及指标 |
| `web/` | 导入、播放器、提问、点读及浏览器播放事件指标 |
| `tools/` | 显式输入演示、评测、录屏转换和真实服务自检 |
| `prompts/` | 参考模板；运行时目前使用 Python 中的提示词 |
| `data/<book_id>/` | 私有原图、文本、生成内容、运行记录与交互音频 |

模型草稿必须声明 `evidence_scope=book|current_frame`，再经过严格类型与页码校验，再独立核对所有事实是否被引用内容支持。任一环节无法确认，就返回 `status=insufficient_evidence` 和明确拒答，不播报未经核对的事实。当前视频/屏幕问题还必须有实际帧及视觉服务，原书文字不能代替。省略 `uncertain` 的旧输出仍兼容，但必须通过引用及证据复核。该复核是模型判断，仍可能出错，不能当作人工复核。

配置视觉和文本服务后，点选后的动作/原因追问使用两路证据：视觉只看实际帧描述动作或姿态，文本只从原书提取相关情节；两路分别复核，都通过才合并为“当前动作/姿态 + 故事里……”并播报。背景复核不会在文本服务失效时改用当前帧。任一路缺证据或失败则拒答，取消时等待并行请求收尾。站立等可见静态姿态可以作答，但不据此编造移动、意图或画外行为。

视觉缓存从旧的“图片+提示词”升级为版本、schema、服务/模型、固定生成参数、图片和提示词指纹；旧缓存不会复用。每页和事件记录标注 `live` 或 `cache_hit`，缓存保留创建时间与schema校验版本，缓存损坏时重新调用；鉴权、额度、审核和不明确的付费提交失败不会无区别重试。

界面正式导入目前使用 `cloud`。本地静态预览是明确标注的降级内容。默认年龄仍为 `6-8`，负责人需在复核环节指定；这两项尚待完善。删除绘本会移除其本机目录。

原始 PDF 文本/文字框、视觉文字框分别保留来源。合并用于文字定位的正文区域时，保留视觉独有的手写正文，匹配时容忍换行和常见引号样式差异，不改写原文。已有书库不会因此自动重跑理解或改写数据。

生成视频仍比较原书与实际视频采样帧，质量拒绝会停止发布。原图像素保护的实际试验发现相机漂移会造成错位，因此当前**只保留为显式诊断工具**，不会自动修改新生成的视频。诊断命令默认只在本机处理既有原片；加 `--review` 才调用已配置的视觉服务，分别检查原片和候选修复，仍可能产生模型调用费用，不提交新生视频任务：

```sh
.venv/bin/python tools/inspect_saved_video_fidelity.py --book-id <本机已有绘本ID> --scene 1
.venv/bin/python tools/inspect_saved_video_fidelity.py --book-id <本机已有绘本ID> --scene 1 --review
```

工具只接受原片任务成功且文件指纹匹配的既有运行；复核在独立 `artifacts/` 目录保存，不修改书库状态。保护区域有覆盖率上限，原片保留。缺少明确对齐结论、出现重影或错位的处理结果不能通过；诊断输出不是可发布作品。

已有失败运行可通过 `tools/assemble_saved_book.py` 和私有指纹清单合成到新目录，复用核对后的视频及完整旁白，不再次提交生成任务。显式启用原图保真动效时，书库标为“混合动画 · 全书测试”，披露替代段落及人工待审状态；原失败运行保持原状。这不是网页自动续跑。字段、压缩旁白选择方式、命令及实测边界见 [全书配音恢复记录](docs/full-book-recovery-2026-11-22.md)。

## 验证

互动回答跟随本轮问题：中文提问用中文回答，英文提问用英文回答，回答语音使用对应语言；自动点选背景按绘本语言。引用其他语言的原文不改变指令语言，用户明确指定回答语言时优先采用。详见 [回答语言验证](docs/response-language-2026-11-23.md)。

已有带配音成片可用 `tools/import_video_book.py` 关联原书 PDF、SRT 和私有时间线清单导入书架。页面标为“导入视频 · 全书测试”，原片音轨保持不变；`--cached-only` 仅复用有效读图缓存，缺页明确标注且不调用模型。命令、数据边界及实测限制见 [既有成片导入记录](docs/imported-video-2026-11-23.md)。

```sh
.venv/bin/python -m pytest -q
.venv/bin/python tools/make_demo_book.py --pdf /path/to/authorized.pdf --book-id unique-demo
.venv/bin/python tools/check_speech_live.py
```

前两项不需要模型费用；第二项只生成最多三页的静态无声预览。必须显式提供获授权 PDF；不扫描数据目录、不搜索测试集、不套用样本书旁白。后面的真实语音自检会调用已配置服务，详见脚本说明。

延迟 API 保留 `measurement_source`、`measurement_kind`、`clock_domain` 与时间戳。浏览器播放、WebAudio 调度都属于代理指标；真实声学记录需要外部采集证据、同一时钟和有效时间戳，预热未确认的样本不纳入预热后汇总。2 秒是当前内部对比目标，最终官方交互延迟门槛尚待通知。

完整进展、验证范围及后续验收见 [docs/development-status.md](docs/development-status.md)。
