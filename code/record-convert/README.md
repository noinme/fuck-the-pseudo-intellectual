# record-convert

将一男一女的单声道对话录音转为带角色、时间戳的 TXT 和 JSON 文件。

流程：

1. 使用 FFmpeg 转为 8 kHz、16-bit、单声道 WAV，并按约一小时切分。
2. 将分片上传到私有 OSS，生成短期签名 URL。
3. 调用阿里云录音文件识别 4.0，并开启 `auto_split` 得到 `ChannelId`。
4. 从每个角色抽取若干 60 秒以内的语音，通过性别识别商用版判断男/女。
5. 合并时间轴，按照输入文件名自动命名 TXT、JSON 和原始响应目录。

## 环境变量

现有变量：

```bash
export ALIYUN_AK_ID='...'
export ALIYUN_AK_SECRET='...'
export NLS_APP_KEY='...'
```

OSS 默认使用此前的 Bucket 和北京地域，也可以显式配置：

```bash
export OSS_BUCKET='record-convert'
export OSS_ENDPOINT='https://oss-cn-beijing.aliyuncs.com'
```

如果使用 STS 临时凭据，还需设置：

```bash
export ALIYUN_SECURITY_TOKEN='...'
```

AccessKey/RAM 账号需要具备该 Bucket 的上传、签名下载和删除权限，并拥有 NLS 录音文件识别及性别识别权限。

## 执行

系统需已安装 `ffmpeg` 和 `ffprobe`。在项目目录运行：

```bash
uv run python main.py 'resources/3.13 和段-重要的事-1.m4a'
```

以示例命令为例，结果会自动命名为：

- `output/3.13 和段-重要的事-1.txt`：适合阅读的带时间戳男女对话稿。
- `output/3.13 和段-重要的事-1.json`：结构化合并结果及性别判定详情。
- `output/3.13 和段-重要的事-1.raw/*.json`：每个分片的阿里云原始响应。

如果同名结果已经存在，程序不会覆盖，而会自动追加 `-2`、`-3` 等序号。

任务全部成功后，程序会删除它上传的 OSS 临时分片和本地工作目录。调试时可保留：

```bash
uv run python main.py 'resources/3.13 和段-重要的事-1.m4a' \
  --keep-remote-chunks --keep-work
```

程序失败或被中断时不会删除 OSS 分片，以免正在执行的云端任务因文件消失而失败；可根据日志中的 `OSS_PREFIX` 到 Bucket 中清理。

每次成功提交全部识别任务后，程序会保存 `output/<输入文件名>.tasks.json`。如果后续查询、性别识别或汇总失败，可复用任务，避免重复计费：

```bash
uv run python main.py 'resources/输入文件.m4a' \
  --resume-tasks 'output/输入文件.tasks.json'
```

`SUCCESS_WITH_NO_VALID_FRAGMENT` 表示某个分片没有检测到有效语音。程序会把它记录为空白时间段并继续处理其他分片，不再因此终止整份录音。
