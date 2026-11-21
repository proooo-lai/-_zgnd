# MiniMax 视频服务配置指引

按 2026-11-21 查阅的官方文档整理。账号可用权限与费用以控制台当时显示为准。

1. 登录 [MiniMax 开放平台](https://platform.minimaxi.com/)，进入控制台，按提示完成账户准备。
2. 通过 **接口密钥 → 创建新的 API Key** 获取按量付费 API Key；视频联调使用这个入口。通过 **账户管理 → 余额** 查看可用余额，按需充值；不需要为本步骤另购语言模型订阅套餐。
3. 确认按量付费服务可调用 `MiniMax-H3-Lite`。官方 [视频 V2 文档](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-create) 将该模型列为可用模型；具体账号仍需真实小样验证。
4. 打开本机工作台的 **服务配置 → 生视频**，填写下面的配置，点击 **保存到本机**。

| 字段 | 填写值 |
|---|---|
| 服务类型 / kind | `minimax` |
| 接口地址 / base_url | `https://api.minimax.cn` |
| 模型 / model | `MiniMax-H3-Lite` |
| 密钥 / api_key | 第 2 步创建的 API Key，仅填写在本机密钥框 |

接口地址填服务根地址，程序会追加 `/v2/video_generation`。不要把完整任务接口填到根地址里。密钥不要发送到聊天、截图、源码或 GitHub。

该模型支持 4–15 秒、480P/768P；当前程序请求 768P，并按参考图选择明确的画面比例，因为 Lite 不支持 `adaptive`。参数支持不等于真实调用已通过。首次联调先生成一个短片，检查构图、文字与角色，再安排整书生成；视频生成会消耗按量额度，具体价格查看控制台。

保存后告诉开发助手“视频配置已保存”即可。若控制台没有该模型或提示权限不足，只需提供模型名称及已隐藏密钥的错误信息。

官方来源：[前置准备](https://platform.minimaxi.com/docs/guides/quickstart-preparation)、[视频生成 V2](https://platform.minimaxi.com/docs/api-reference/video-generation-v2-create)。
