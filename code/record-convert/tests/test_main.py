import tempfile
import unittest
from pathlib import Path

import main


class TranscriptTests(unittest.TestCase):
    def test_safe_file_stem(self):
        self.assertEqual(main.safe_file_stem(Path("一次对话.m4a")), "一次对话")
        self.assertEqual(main.safe_file_stem(Path("bad:name?.wav")), "bad_name_")

    def test_choose_output_stem_avoids_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "一次对话.txt").touch()
            self.assertEqual(
                main.choose_output_stem(output_dir, Path("一次对话.m4a")),
                "一次对话-2",
            )

    def test_format_timestamp(self):
        self.assertEqual(main.format_timestamp(7_344_149), "02:02:24.149")

    def test_choose_gender_samples_groups_and_limits(self):
        sentences = [
            {"ChannelId": 0, "BeginTime": 0, "EndTime": 1_000},
            {"ChannelId": 0, "BeginTime": 0, "EndTime": 6_000},
            {"ChannelId": 0, "BeginTime": 0, "EndTime": 4_000},
            {"ChannelId": 1, "BeginTime": 10_000, "EndTime": 15_000},
        ]
        samples = main.choose_gender_samples(sentences, max_samples=1)
        self.assertEqual(samples[0][0]["EndTime"], 6_000)
        self.assertEqual(samples[1][0]["ChannelId"], 1)

    def test_merge_sentences_offsets_second_chunk(self):
        chunks = [
            main.AudioChunk(0, Path("a.wav"), 0, 3_600_000),
            main.AudioChunk(1, Path("b.wav"), 3_600_000, 100_000),
        ]
        responses = [
            {"Result": {"Sentences": [{"ChannelId": 0, "BeginTime": 10, "EndTime": 20, "Text": "甲"}]}},
            {"Result": {"Sentences": [{"ChannelId": 1, "BeginTime": 30, "EndTime": 40, "Text": "乙"}]}},
        ]
        merged = main.merge_sentences(
            chunks, responses, {(0, 0): "男", (1, 1): "女"}
        )
        self.assertEqual(merged[1]["BeginTime"], 3_600_030)
        self.assertEqual(merged[1]["Role"], "女")

    def test_write_outputs(self):
        chunks = [main.AudioChunk(0, Path("a.wav"), 0, 1_000)]
        responses = [
            {"Result": {"Sentences": [{"ChannelId": 0, "BeginTime": 10, "EndTime": 20, "Text": "你好"}]}}
        ]
        with tempfile.TemporaryDirectory() as directory:
            text_path, json_path = main.write_outputs(
                Path(directory),
                "source",
                Path("source.m4a"),
                chunks,
                responses,
                {(0, 0): "男"},
                [],
            )
            self.assertIn("男：你好", text_path.read_text(encoding="utf-8"))
            self.assertEqual(text_path.name, "source.txt")
            self.assertEqual(json_path.name, "source.json")
            self.assertTrue(json_path.is_file())

    def test_output_stem_reserves_task_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            (output_dir / "录音.tasks.json").touch()
            self.assertEqual(
                main.choose_output_stem(output_dir, Path("录音.m4a")), "录音-2"
            )


if __name__ == "__main__":
    unittest.main()
