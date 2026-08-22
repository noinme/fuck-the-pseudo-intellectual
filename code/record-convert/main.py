from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar


REGION_ID = "cn-shanghai"
FILETRANS_DOMAIN = "filetrans.cn-shanghai.aliyuncs.com"
FILETRANS_VERSION = "2018-08-17"
NLS_GATEWAY = "wss://nls-gateway.cn-shanghai.aliyuncs.com/ws/v1"
SAMPLE_RATE = 8_000
CHUNK_SECONDS = 3_600
POLL_SECONDS = 10
SIGNED_URL_SECONDS = 6 * 60 * 60
ACS_CONNECT_TIMEOUT_SECONDS = 10
ACS_READ_TIMEOUT_SECONDS = 30
NETWORK_RETRY_ATTEMPTS = 4


T = TypeVar("T")


@dataclass(frozen=True)
class Settings:
    access_key_id: str
    access_key_secret: str
    app_key: str
    oss_endpoint: str
    oss_bucket: str
    oss_prefix: str
    security_token: str | None


@dataclass(frozen=True)
class AudioChunk:
    index: int
    path: Path
    offset_ms: int
    duration_ms: int


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}")
    return value


def retry_call(
    action: Callable[[], T],
    description: str,
    attempts: int = NETWORK_RETRY_ATTEMPTS,
    delay_seconds: float = 3,
) -> T:
    """重试可安全重复的网络调用，不输出可能含凭据的异常正文。"""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:
            if attempt == attempts:
                raise
            print(
                f"  警告：{description}网络失败（{type(exc).__name__}），"
                f"{delay_seconds:g} 秒后重试 {attempt}/{attempts - 1}……"
            )
            time.sleep(delay_seconds)
    raise AssertionError("retry_call reached an unreachable state")


def safe_file_stem(input_path: Path) -> str:
    stem = input_path.stem.strip().rstrip(".")
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)
    return stem[:160] or "transcript"


def choose_output_stem(output_dir: Path, input_path: Path) -> str:
    """按输入文件名生成不覆盖已有结果的输出名。"""
    base = safe_file_stem(input_path)
    candidate = base
    suffix = 2
    while any(
        (
            (output_dir / f"{candidate}.txt").exists(),
            (output_dir / f"{candidate}.json").exists(),
            (output_dir / f"{candidate}.raw").exists(),
            (output_dir / f"{candidate}.tasks.json").exists(),
        )
    ):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def load_settings() -> Settings:
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    return Settings(
        access_key_id=required_env("ALIYUN_AK_ID"),
        access_key_secret=required_env("ALIYUN_AK_SECRET"),
        app_key=required_env("NLS_APP_KEY"),
        oss_endpoint=os.getenv(
            "OSS_ENDPOINT", "https://oss-cn-beijing.aliyuncs.com"
        ),
        oss_bucket=os.getenv("OSS_BUCKET", "record-convert"),
        oss_prefix=os.getenv("OSS_PREFIX", f"record-convert/chunks/{run_id}"),
        security_token=os.getenv("ALIYUN_SECURITY_TOKEN"),
    )


def run_command(args: list[str]) -> None:
    subprocess.run(args, check=True)


def ffprobe_duration_ms(path: Path) -> int:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return round(float(result.stdout.strip()) * 1000)


def split_audio(input_path: Path, work_dir: Path) -> list[AudioChunk]:
    chunk_dir = work_dir / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    pattern = chunk_dir / "chunk-%02d.wav"
    print("[1/6] 转为 8 kHz/16-bit/单声道，并按约 1 小时切分……")
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(input_path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-sample_fmt",
            "s16",
            "-f",
            "segment",
            "-segment_time",
            str(CHUNK_SECONDS),
            "-reset_timestamps",
            "1",
            str(pattern),
        ]
    )
    paths = sorted(chunk_dir.glob("chunk-*.wav"))
    if not paths:
        raise RuntimeError("FFmpeg 没有生成音频分片")

    chunks: list[AudioChunk] = []
    offset_ms = 0
    for index, path in enumerate(paths):
        duration_ms = ffprobe_duration_ms(path)
        chunks.append(AudioChunk(index, path, offset_ms, duration_ms))
        offset_ms += duration_ms
    print(
        "  已生成："
        + "，".join(f"{c.path.name} ({format_timestamp(c.duration_ms)})" for c in chunks)
    )
    return chunks


def make_oss_bucket(settings: Settings):
    import oss2

    if settings.security_token:
        auth = oss2.StsAuth(
            settings.access_key_id,
            settings.access_key_secret,
            settings.security_token,
        )
    else:
        auth = oss2.Auth(settings.access_key_id, settings.access_key_secret)
    return oss2.Bucket(auth, settings.oss_endpoint, settings.oss_bucket)


def make_acs_client(settings: Settings):
    from aliyunsdkcore.auth.credentials import StsTokenCredential
    from aliyunsdkcore.client import AcsClient

    if settings.security_token:
        credential = StsTokenCredential(
            settings.access_key_id,
            settings.access_key_secret,
            settings.security_token,
        )
        return AcsClient(
            region_id=REGION_ID,
            credential=credential,
            connect_timeout=ACS_CONNECT_TIMEOUT_SECONDS,
            timeout=ACS_READ_TIMEOUT_SECONDS,
        )
    return AcsClient(
        settings.access_key_id,
        settings.access_key_secret,
        REGION_ID,
        connect_timeout=ACS_CONNECT_TIMEOUT_SECONDS,
        timeout=ACS_READ_TIMEOUT_SECONDS,
    )


def create_nls_token(settings: Settings) -> str:
    from aliyunsdkcore.request import CommonRequest

    request = CommonRequest()
    request.set_method("POST")
    request.set_domain("nls-meta.cn-shanghai.aliyuncs.com")
    request.set_version("2019-02-28")
    request.set_action_name("CreateToken")
    request.set_protocol_type("https")
    client = make_acs_client(settings)
    response = json.loads(
        retry_call(
            lambda: client.do_action_with_exception(request),
            "创建 NLS Token 时",
        )
    )
    token = response.get("Token", {}).get("Id")
    if not token:
        raise RuntimeError(f"获取 NLS Token 失败：{redact_response(response)}")
    return str(token)


def upload_chunks(settings: Settings, chunks: list[AudioChunk]):
    bucket = make_oss_bucket(settings)
    uploaded: list[tuple[str, str]] = []
    attempted_keys: list[str] = []
    print("[2/6] 上传分片到私有 OSS……")
    print(f"  OSS 临时目录：oss://{settings.oss_bucket}/{settings.oss_prefix.rstrip('/')}/")
    try:
        for chunk in chunks:
            object_key = f"{settings.oss_prefix.rstrip('/')}/{chunk.path.name}"
            attempted_keys.append(object_key)
            retry_call(
                lambda: bucket.put_object_from_file(object_key, str(chunk.path)),
                f"上传 {chunk.path.name} 时",
            )
            signed_url = bucket.sign_url(
                "GET", object_key, SIGNED_URL_SECONDS, slash_safe=True
            )
            uploaded.append((object_key, signed_url))
            print(f"  已上传 {chunk.path.name}（链接已隐藏）")
    except Exception:
        print("  上传未完成，清理本次可能已写入的 OSS 分片……")
        for object_key in attempted_keys:
            retry_call(
                lambda key=object_key: bucket.delete_object(key),
                "清理上传失败的 OSS 分片时",
            )
        raise
    return bucket, uploaded


class FileTransClient:
    def __init__(self, settings: Settings):
        self.client = make_acs_client(settings)
        self.app_key = settings.app_key

    @staticmethod
    def _request(action: str, method: str):
        from aliyunsdkcore.request import CommonRequest

        request = CommonRequest()
        request.set_domain(FILETRANS_DOMAIN)
        request.set_version(FILETRANS_VERSION)
        request.set_product("nls-filetrans")
        request.set_action_name(action)
        request.set_method(method)
        request.set_protocol_type("https")
        return request

    def submit(self, signed_url: str) -> str:
        task = {
            "appkey": self.app_key,
            "file_link": signed_url,
            "version": "4.0",
            "enable_words": False,
            "auto_split": True,
            "enable_punctuation_prediction": True,
            "enable_inverse_text_normalization": True,
        }
        request = self._request("SubmitTask", "POST")
        request.add_body_params("Task", json.dumps(task, ensure_ascii=False))
        response = json.loads(self.client.do_action_with_exception(request))
        if response.get("StatusText") != "SUCCESS" or not response.get("TaskId"):
            raise RuntimeError(f"提交识别任务失败：{redact_response(response)}")
        return str(response["TaskId"])

    def wait(self, task_id: str, timeout_seconds: int) -> dict[str, Any]:
        request = self._request("GetTaskResult", "GET")
        request.add_query_param("TaskId", task_id)
        deadline = time.monotonic() + timeout_seconds
        previous_status = None
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待识别任务 {task_id} 超时")
            response = json.loads(
                retry_call(
                    lambda: self.client.do_action_with_exception(request),
                    f"查询识别任务 {task_id} 时",
                )
            )
            status = response.get("StatusText")
            if status != previous_status:
                print(f"  任务 {task_id}: {status}")
                previous_status = status
            if status not in {"QUEUEING", "RUNNING"}:
                if status == "SUCCESS_WITH_NO_VALID_FRAGMENT":
                    print(f"  任务 {task_id}: 未检测到有效语音，按空白分片继续")
                    response.setdefault("Result", {"Sentences": []})
                    return response
                if status != "SUCCESS":
                    raise RuntimeError(
                        f"识别任务 {task_id} 失败：{redact_response(response)}"
                    )
                return response
            time.sleep(POLL_SECONDS)


def redact_response(response: dict[str, Any]) -> str:
    safe = {
        key: value
        for key, value in response.items()
        if key not in {"Result", "AccessKeyId", "Signature"}
    }
    return json.dumps(safe, ensure_ascii=False)


def transcribe_chunks(
    settings: Settings,
    chunks: list[AudioChunk],
    uploads: list[tuple[str, str]],
    raw_dir: Path,
    timeout_seconds: int,
    existing_task_ids: list[str] | None = None,
    manifest_path: Path | None = None,
    manifest_metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    client = FileTransClient(settings)
    submitted: list[tuple[AudioChunk, str]] = []
    if existing_task_ids is not None:
        if len(existing_task_ids) != len(chunks):
            raise RuntimeError(
                f"恢复清单包含 {len(existing_task_ids)} 个任务，但音频切出了 {len(chunks)} 个分片"
            )
        print("[3/6] 使用恢复清单中的已有任务，不重复提交……")
        submitted = list(zip(chunks, existing_task_ids, strict=True))
        for chunk, task_id in submitted:
            print(f"  {chunk.path.name}: {task_id}")
    else:
        print("[3/6] 提交录音识别任务（启用智能分轨）……")
        for chunk, (_, signed_url) in zip(chunks, uploads, strict=True):
            task_id = client.submit(signed_url)
            submitted.append((chunk, task_id))
            print(f"  {chunk.path.name}: {task_id}")

        if manifest_path is not None:
            manifest = dict(manifest_metadata or {})
            manifest.update(
                {
                    "task_ids": [task_id for _, task_id in submitted],
                    "object_keys": [object_key for object_key, _ in uploads],
                }
            )
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"  已保存任务恢复清单：{manifest_path}")

    print("[4/6] 等待识别完成……")
    raw_dir.mkdir(parents=True, exist_ok=True)
    responses: list[dict[str, Any]] = []
    for chunk, task_id in submitted:
        response = client.wait(task_id, timeout_seconds)
        response["_chunk_index"] = chunk.index
        response["_chunk_offset_ms"] = chunk.offset_ms
        raw_path = raw_dir / f"chunk-{chunk.index:02d}-{task_id}.json"
        raw_path.write_text(
            json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        responses.append(response)
    return responses


def chunk_sentences(response: dict[str, Any]) -> list[dict[str, Any]]:
    result = response.get("Result") or {}
    sentences = result.get("Sentences") or []
    if not isinstance(sentences, list):
        raise RuntimeError("识别响应中的 Result.Sentences 格式不正确")
    return sentences


def choose_gender_samples(
    sentences: list[dict[str, Any]], max_samples: int = 3
) -> dict[int, list[dict[str, Any]]]:
    by_channel: dict[int, list[dict[str, Any]]] = {}
    for sentence in sentences:
        channel = int(sentence.get("ChannelId", 0))
        begin = int(sentence.get("BeginTime", 0))
        end = int(sentence.get("EndTime", 0))
        duration = end - begin
        if 2_500 <= duration <= 30_000:
            by_channel.setdefault(channel, []).append(sentence)
    for channel, candidates in by_channel.items():
        candidates.sort(
            key=lambda item: int(item["EndTime"]) - int(item["BeginTime"]),
            reverse=True,
        )
        by_channel[channel] = candidates[:max_samples]
    return by_channel


def extract_pcm(source: Path, sentence: dict[str, Any], output: Path) -> None:
    begin_seconds = int(sentence["BeginTime"]) / 1000
    duration_seconds = (int(sentence["EndTime"]) - int(sentence["BeginTime"])) / 1000
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{begin_seconds:.3f}",
            "-t",
            f"{duration_seconds:.3f}",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            str(output),
        ]
    )


class GenderIdentifier:
    """阿里云 GenderIdentification 通用 WebSocket 请求。"""

    def __init__(self, token: str, app_key: str):
        self.token = token
        self.app_key = app_key

    def identify(self, pcm_path: Path) -> dict[str, Any]:
        from nls import util
        from nls.core import NlsCore
        from nls import websocket as nls_websocket

        started = threading.Event()
        completed = threading.Event()
        failed = threading.Event()
        events: list[dict[str, Any]] = []
        error_messages: list[str] = []

        def on_message(message: str, *_: Any) -> None:
            data = json.loads(message)
            name = data.get("header", {}).get("name")
            if name == "TaskStarted":
                started.set()
            elif name == "TaskResult":
                events.append(data)
            elif name == "TaskCompleted":
                completed.set()
            elif name == "TaskFailed":
                error_messages.append(redact_response(data.get("header", {})))
                failed.set()
                started.set()
                completed.set()

        def on_error(message: Any, *_: Any) -> None:
            error_messages.append(str(message))
            failed.set()
            started.set()
            completed.set()

        core = NlsCore(
            url=NLS_GATEWAY,
            token=self.token,
            on_open=lambda *_: None,
            on_message=on_message,
            on_close=lambda *_: None,
            on_error=on_error,
        )
        # 官方 SDK 默认打开 WebSocket trace，会把 Token 打到日志中；必须关闭。
        nls_websocket.enableTrace(False)
        task_id = uuid.uuid4().hex

        def message(name: str, payload: dict[str, Any] | None = None) -> str:
            body: dict[str, Any] = {
                "header": {
                    "message_id": uuid.uuid4().hex,
                    "task_id": task_id,
                    "namespace": "GenderIdentification",
                    "name": name,
                    "appkey": self.app_key,
                },
                "context": util.GetDefaultContext(),
            }
            if payload is not None:
                body["payload"] = payload
            return json.dumps(body)

        try:
            core.start(
                message("StartTask", {"format": "pcm", "sample_rate": SAMPLE_RATE}),
                ping_interval=8,
                ping_timeout=None,
            )
            if not started.wait(15) or failed.is_set():
                raise RuntimeError("性别识别任务启动失败：" + "; ".join(error_messages))

            with pcm_path.open("rb") as stream:
                while data := stream.read(3_200):
                    core.send(data, True)
                    time.sleep(0.1)

            core.send(message("StopTask"), False)
            if not completed.wait(20) or failed.is_set():
                raise RuntimeError("性别识别任务失败：" + "; ".join(error_messages))
        finally:
            core.shutdown()

        if not events:
            return {"type": 0, "score": None, "task_id": task_id}
        best = max(events, key=lambda item: float(item.get("payload", {}).get("score", -1000)))
        payload = best.get("payload", {})
        return {
            "type": int(payload.get("type", 0)),
            "score": payload.get("score"),
            "task_id": task_id,
        }


def identify_genders(
    settings: Settings,
    chunks: list[AudioChunk],
    responses: list[dict[str, Any]],
    work_dir: Path,
) -> tuple[dict[tuple[int, int], str], list[dict[str, Any]]]:
    print("[5/6] 抽取角色样本并识别性别……")
    token = create_nls_token(settings)
    identifier = GenderIdentifier(token, settings.app_key)
    gender_dir = work_dir / "gender-samples"
    gender_dir.mkdir(parents=True, exist_ok=True)
    labels: dict[tuple[int, int], str] = {}
    strengths: dict[tuple[int, int], tuple[int, float]] = {}
    details: list[dict[str, Any]] = []
    type_labels = {0: "未知", 2: "女", 3: "男"}

    for chunk, response in zip(chunks, responses, strict=True):
        samples = choose_gender_samples(chunk_sentences(response))
        for channel, sentences in sorted(samples.items()):
            results: list[dict[str, Any]] = []
            for sample_index, sentence in enumerate(sentences):
                pcm_path = gender_dir / (
                    f"chunk-{chunk.index:02d}-channel-{channel}-sample-{sample_index}.pcm"
                )
                extract_pcm(chunk.path, sentence, pcm_path)
                result = retry_call(
                    lambda: identifier.identify(pcm_path),
                    f"识别 {chunk.path.name} / ChannelId {channel} 性别时",
                    attempts=3,
                )
                result.update(
                    {
                        "chunk_index": chunk.index,
                        "channel_id": channel,
                        "begin_time": sentence["BeginTime"],
                        "end_time": sentence["EndTime"],
                    }
                )
                results.append(result)
                details.append(result)

            recognized = [item for item in results if item["type"] in {2, 3}]
            if recognized:
                votes: dict[int, tuple[int, float]] = {}
                for item in recognized:
                    gender_type = int(item["type"])
                    count, score = votes.get(gender_type, (0, 0.0))
                    votes[gender_type] = (
                        count + 1,
                        score + float(item.get("score") or -1000),
                    )
                gender_type = max(votes, key=lambda key: votes[key])
                labels[(chunk.index, channel)] = type_labels[gender_type]
                strengths[(chunk.index, channel)] = votes[gender_type]
            else:
                labels[(chunk.index, channel)] = f"角色{channel + 1}"
                strengths[(chunk.index, channel)] = (0, float("-inf"))

        channels = sorted(samples)
        if len(channels) == 2:
            keys = [(chunk.index, channel) for channel in channels]
            if {labels[key] for key in keys} != {"男", "女"}:
                recognized_keys = [key for key in keys if labels[key] in {"男", "女"}]
                if recognized_keys:
                    anchor = max(recognized_keys, key=lambda key: strengths[key])
                    other = keys[1] if keys[0] == anchor else keys[0]
                    labels[other] = "女" if labels[anchor] == "男" else "男"
        elif len(channels) < 2:
            print(f"  警告：{chunk.path.name} 只检测到一个角色音轨，无法完整区分男女")

        for channel in channels:
            print(
                f"  {chunk.path.name} / ChannelId {channel}: "
                f"{labels[(chunk.index, channel)]}"
            )
    return labels, details


def merge_sentences(
    chunks: list[AudioChunk],
    responses: list[dict[str, Any]],
    labels: dict[tuple[int, int], str],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for chunk, response in zip(chunks, responses, strict=True):
        for sentence in chunk_sentences(response):
            channel = int(sentence.get("ChannelId", 0))
            item = dict(sentence)
            item["BeginTime"] = chunk.offset_ms + int(sentence.get("BeginTime", 0))
            item["EndTime"] = chunk.offset_ms + int(sentence.get("EndTime", 0))
            item["Role"] = labels.get(
                (chunk.index, channel), f"角色{channel + 1}"
            )
            item["ChunkIndex"] = chunk.index
            merged.append(item)
    merged.sort(key=lambda item: (item["BeginTime"], item["EndTime"]))
    return merged


def format_timestamp(milliseconds: int) -> str:
    total_seconds, millis = divmod(max(0, milliseconds), 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def write_outputs(
    output_dir: Path,
    output_stem: str,
    input_path: Path,
    chunks: list[AudioChunk],
    responses: list[dict[str, Any]],
    labels: dict[tuple[int, int], str],
    gender_details: list[dict[str, Any]],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    merged = merge_sentences(chunks, responses, labels)
    json_path = output_dir / f"{output_stem}.json"
    text_path = output_dir / f"{output_stem}.txt"
    json_path.write_text(
        json.dumps(
            {
                "source": str(input_path),
                "duration_ms": sum(chunk.duration_ms for chunk in chunks),
                "role_labels": [
                    {
                        "chunk_index": chunk_index,
                        "channel_id": channel,
                        "role": role,
                    }
                    for (chunk_index, channel), role in sorted(labels.items())
                ],
                "gender_identification": gender_details,
                "sentences": merged,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    lines = [
        f"[{format_timestamp(int(item['BeginTime']))}] {item['Role']}：{item.get('Text', '')}"
        for item in merged
    ]
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return text_path, json_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将双人对话录音转为带男女角色和时间戳的文字稿"
    )
    parser.add_argument("input", type=Path, help="本地音频文件")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output"), help="结果目录"
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="临时工作目录；默认根据输入文件自动生成",
    )
    parser.add_argument(
        "--timeout-hours",
        type=float,
        default=3.0,
        help="每个录音识别任务的最长等待时间",
    )
    parser.add_argument(
        "--keep-remote-chunks",
        action="store_true",
        help="成功后仍保留程序上传到 OSS 的音频分片",
    )
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="成功后保留本地临时分片和性别样本",
    )
    parser.add_argument(
        "--resume-tasks",
        type=Path,
        default=None,
        help="使用任务恢复清单继续处理，不重新上传或提交识别任务",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"找不到音频文件：{input_path}")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("需要先安装 ffmpeg 和 ffprobe")
    resume_manifest: dict[str, Any] | None = None
    if args.resume_tasks is not None:
        resume_path = args.resume_tasks.expanduser().resolve()
        resume_manifest = json.loads(resume_path.read_text(encoding="utf-8"))
        output_stem = str(
            resume_manifest.get("output_stem") or safe_file_stem(input_path)
        )
    else:
        output_stem = choose_output_stem(output_dir, input_path)
    if args.work_dir is None:
        work_dir = (
            Path(".record-convert-work")
            / f"{output_stem}-{uuid.uuid4().hex[:8]}"
        ).resolve()
    else:
        work_dir = args.work_dir.expanduser().resolve()
    if work_dir == input_path.parent or work_dir == output_dir:
        raise RuntimeError("工作目录不能与输入目录或输出目录相同")

    print(f"输入文件：{input_path}")
    print(f"输出名称：{output_stem}.txt / {output_stem}.json")
    settings = load_settings()
    work_dir.mkdir(parents=True, exist_ok=True)
    chunks = split_audio(input_path, work_dir)
    manifest_path = output_dir / f"{output_stem}.tasks.json"
    if resume_manifest is not None:
        task_ids = [str(value) for value in resume_manifest.get("task_ids", [])]
        object_keys = [str(value) for value in resume_manifest.get("object_keys", [])]
        if len(object_keys) != len(chunks):
            raise RuntimeError("恢复清单中的 object_keys 数量与音频分片数量不一致")
        bucket = make_oss_bucket(settings)
        uploads = [(object_key, "") for object_key in object_keys]
        print("[2/6] 恢复模式：跳过 OSS 上传，复用已有私有分片")
    else:
        task_ids = None
        bucket, uploads = upload_chunks(settings, chunks)
    completed = False
    try:
        responses = transcribe_chunks(
            settings,
            chunks,
            uploads,
            output_dir / f"{output_stem}.raw",
            timeout_seconds=round(args.timeout_hours * 3600),
            existing_task_ids=task_ids,
            manifest_path=manifest_path,
            manifest_metadata={
                "version": 1,
                "input": str(input_path),
                "output_stem": output_stem,
                "oss_bucket": settings.oss_bucket,
                "oss_endpoint": settings.oss_endpoint,
            },
        )
        labels, gender_details = identify_genders(
            settings, chunks, responses, work_dir
        )
        print("[6/6] 合并结果并写入文件……")
        text_path, json_path = write_outputs(
            output_dir,
            output_stem,
            input_path,
            chunks,
            responses,
            labels,
            gender_details,
        )
        completed = True
        print(f"完成：{text_path}")
        print(f"完成：{json_path}")
    finally:
        if completed and not args.keep_remote_chunks:
            for object_key, _ in uploads:
                retry_call(
                    lambda key=object_key: bucket.delete_object(key),
                    "删除 OSS 临时分片时",
                )
            print("已删除程序上传的 OSS 临时分片")
        if completed and not args.keep_work:
            shutil.rmtree(work_dir, ignore_errors=True)
    return 0


def cli() -> int:
    """命令行入口；避免第三方 SDK traceback 泄露签名 URL 或凭据标识。"""
    try:
        return main()
    except KeyboardInterrupt:
        print("已中断；已上传的临时分片未自动删除，请按 OSS_PREFIX 检查。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            f"错误：{type(exc).__name__}（异常详情已隐藏，避免泄露凭据或签名 URL）",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
