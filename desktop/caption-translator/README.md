# 字幕翻译

独立的 macOS 小工具。选一个显示英文字幕的窗口、框出字幕区域，在置顶窗口里阅读随原文更新的中文。

要求 macOS 26 或更新版本。窗口捕获使用 ScreenCaptureKit，英文识别使用 Vision，英中翻译使用 Apple Translation。无第三方依赖、API 密钥或 Jarvis 服务依赖。

## 使用

打开 `dist/Caption Translator.app`：

1. 点击「选择窗口」，在 macOS 的系统选择器里选择字幕所在的窗口，并确认系统的共享提示。
2. 在预览中拖出字幕区域，避开头像、工具栏和其他文字；也可以使用整个窗口。
3. 点击「开始翻译」。使用「暂停翻译」停止捕获，已有译文会保留。
4. 图钉控制置顶。更多菜单可以显示/隐藏英文、调整字号、复制中文。快捷键：⌘O 选窗口，⌘R 开始/暂停，⇧⌘P 置顶。

首次翻译可能需要通过系统提示下载英中语言包。下载完成后可在本机翻译。若捕获权限被拒绝，按应用错误提示在系统设置中授权后重新启动应用。受保护的视频内容、最小化或关闭的窗口可能无法捕获。

窗口可以移动；改变窗口大小或字幕布局后请重新框选。当前版本固定为英语到简体中文。OCR 仍可能识别错字，翻译保留原转写中的错误与不完整句子，不应把其输出当作原发言的精确记录。

只把最近最多三句、上限 110 个英文词送入本地翻译。新译文完成前保留旧译文；滚动重叠内容和未完成的句子在内存里合并，不重复追加到界面。状态栏显示的翻译用时仅为翻译调用时间，不是从发言到显示的总延迟。

应用不保存截图、字幕或音频，不调用外部翻译 API。开发用回放验证只在显式提供 `--evidence` 参数时写本地验证报告。

## 构建

需要 Xcode 的 Swift 6.2 或更新工具链：

```sh
./scripts/build-app.sh
open 'dist/Caption Translator.app'
```

脚本生成本机可运行、临时签名的 `.app`，尚未做 Developer ID 签名或公证。要在其他机器分发，需要另行签名、公证。

## 验证

```sh
CLANG_MODULE_CACHE_PATH="$PWD/.build/clang-cache" swift test --disable-sandbox -debug-info-format none --cache-path "$PWD/.build/cache"
```

`CaptionProbe` 是针对本次用户提供录屏的验收工具：逐半秒取帧，真实运行 Vision、滚动文本合并和 Apple Translation，并输出 JSON。语言包未安装时可加 `--ocr-only` 只验证识别。需在正常 macOS 会话运行，受限执行沙箱可能阻止系统视频解码。

```sh
.build/release/CaptionProbe /absolute/path/to/recording.mov
open -n 'dist/Caption Translator.app' --args --replay /absolute/path/to/recording.mov
```

验收记录保存在本机忽略目录 `dist/verification/`。已通过滚动重叠、断行、修订、短句增长、空帧去重和上下文长度的单元测试，并用提供的 9 秒录屏验证了实际 OCR、翻译与应用回放。首次 OCR 模型加载明显慢于热身后的识别，性能数字只代表这份录屏在本机的结果。
