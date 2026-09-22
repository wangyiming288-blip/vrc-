# vrc-
一个关于vrchat中可以用除了网易和qq音乐的播放歌词和歌词进度的项目
酷狗音乐
   │ SMTC（Windows 系统媒体传输控件）
   ▼
Python 脚本 ──► 在线歌词源（网易云 / lrclib）拿 LRC
   │
   │ OSC UDP → 127.0.0.1:9000
   ▼
VRChat
   ├─ /chatbox/input          显示歌名 / 进度 / 歌词
   └─ /avatar/parameters/xxx  可选：数值给 Avatar 用
三、使用步骤
1. 装环境（Python 3.9 ~ 3.12，3.13 可能没有预编译包）

pip install python-osc requests winsdk
如果 winsdk 装不上：

pip install winrt-runtime winrt-Windows.Media.Control ^
            winrt-Windows.Foundation winrt-Windows.Foundation.Collections
            2. 确认酷狗在播歌

打开酷狗放一首歌，按一下键盘的多媒体键（或者点开 Win11 的音量面板），看能不能看到酷狗的歌名。能就说明 SMTC 正常。

3. 启动脚本

python kugou_osc.py
正常的话控制台会打印：

[*] 已启动，正在监听播放器… (OSC → 127.0.0.1:9000)

[♪] 夜曲 - 周杰伦
  [歌词] 命中源: netease
  已加载歌词 48 行
  4. VRChat 端

确保 VRChat 的 OSC 开启（现在版本默认开启，在 Settings → OSC 里可以看到）
进游戏后，你头顶的聊天框就会显示：
♪ 夜曲 - 周杰伦
[=====>------] 1:23/3:45
一群嗜血的蚂蚁 被腐肉所吸引
