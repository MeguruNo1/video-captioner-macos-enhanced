import unittest

from app.core.bk_asr.mlx_workflow import (
    build_chunk_windows,
    merge_transcription_results,
    offset_transcription_result,
    split_ranges_to_windows,
)


class MLXWhisperWorkflowTests(unittest.TestCase):
    def test_builds_overlapping_chunk_windows_with_keep_ranges(self):
        windows = build_chunk_windows(
            duration_seconds=1250,
            chunk_duration_seconds=600,
            overlap_seconds=30,
        )

        self.assertEqual(
            windows,
            [
                (0.0, 600.0, 0.0, 585.0),
                (570.0, 1170.0, 585.0, 1155.0),
                (1140.0, 1250.0, 1155.0, 1250.0),
            ],
        )

    def test_offsets_result_to_global_timestamps_and_filters_overlap(self):
        result = offset_transcription_result(
            {
                "segments": [
                    {
                        "start": 0.1,
                        "end": 5.0,
                        "text": "early",
                        "words": [{"word": "early", "start": 0.1, "end": 0.4}],
                    },
                    {
                        "start": 31.0,
                        "end": 33.0,
                        "text": "kept",
                        "words": [{"word": "kept", "start": 31.0, "end": 33.0}],
                    },
                ]
            },
            offset_seconds=570.0,
            keep_start_seconds=585.0,
            keep_end_seconds=1155.0,
        )

        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["text"], "kept")
        self.assertEqual(result["segments"][0]["start"], 601.0)
        self.assertEqual(result["segments"][0]["words"][0]["start"], 601.0)

    def test_offsets_result_filters_words_without_dropping_boundary_segment(self):
        result = offset_transcription_result(
            {
                "segments": [
                    {
                        "start": 10.0,
                        "end": 40.0,
                        "text": "drop keep",
                        "words": [
                            {"word": "drop", "start": 10.0, "end": 10.4},
                            {"word": "keep", "start": 31.0, "end": 31.5},
                        ],
                    },
                ]
            },
            offset_seconds=570.0,
            keep_start_seconds=600.0,
            keep_end_seconds=650.0,
        )

        self.assertEqual(len(result["segments"]), 1)
        self.assertEqual(result["segments"][0]["text"], "keep")
        self.assertEqual(result["segments"][0]["start"], 601.0)
        self.assertEqual(result["segments"][0]["end"], 601.5)

    def test_merges_results_and_dedupes_overlap_words(self):
        merged = merge_transcription_results(
            [
                {
                    "segments": [
                        {
                            "start": 0.0,
                            "end": 1.0,
                            "text": "hello",
                            "words": [{"word": "hello", "start": 0.0, "end": 1.0}],
                        }
                    ]
                },
                {
                    "segments": [
                        {
                            "start": 0.05,
                            "end": 1.05,
                            "text": "hello",
                            "words": [{"word": "hello", "start": 0.05, "end": 1.05}],
                        },
                        {
                            "start": 1.2,
                            "end": 2.0,
                            "text": "world",
                            "words": [{"word": "world", "start": 1.2, "end": 2.0}],
                        },
                    ]
                },
            ]
        )

        self.assertEqual([word["word"] for word in merged["segments"][0]["words"]], ["hello"])
        self.assertEqual([word["word"] for word in merged["segments"][1]["words"]], ["world"])

    def test_merges_padded_vad_ranges_before_building_windows(self):
        windows = split_ranges_to_windows(
            ranges=[(10.0, 11.0), (11.5, 12.5)],
            duration_seconds=30.0,
            chunk_duration_seconds=600,
            overlap_seconds=30,
        )

        self.assertEqual(windows, [(9.0, 13.5, 9.0, 13.5)])

    def test_dedupes_words_across_overlapping_segments(self):
        merged = merge_transcription_results(
            [
                {
                    "segments": [
                        {
                            "start": 1.0,
                            "end": 1.4,
                            "text": "I do",
                            "words": [
                                {"word": "I", "start": 1.0, "end": 1.2},
                                {"word": "do", "start": 1.2, "end": 1.4},
                            ],
                        }
                    ]
                },
                {
                    "segments": [
                        {
                            "start": 1.02,
                            "end": 1.7,
                            "text": "I do believe",
                            "words": [
                                {"word": "I", "start": 1.02, "end": 1.22},
                                {"word": "do", "start": 1.22, "end": 1.42},
                                {"word": "believe", "start": 1.45, "end": 1.7},
                            ],
                        }
                    ]
                },
            ]
        )

        words = [
            word["word"]
            for segment in merged["segments"]
            for word in segment.get("words", [])
        ]
        self.assertEqual(words, ["I", "do", "believe"])

    def test_keeps_intentional_sequential_repeated_words(self):
        merged = merge_transcription_results(
            [
                {
                    "segments": [
                        {
                            "start": 1.0,
                            "end": 1.4,
                            "text": "very very",
                            "words": [
                                {"word": "very", "start": 1.0, "end": 1.2},
                                {"word": "very", "start": 1.2, "end": 1.4},
                            ],
                        }
                    ]
                }
            ]
        )

        self.assertEqual(
            [word["word"] for word in merged["segments"][0]["words"]],
            ["very", "very"],
        )

    def test_keeps_intentional_sequential_repeated_segments(self):
        merged = merge_transcription_results(
            [
                {
                    "segments": [
                        {
                            "start": 1.0,
                            "end": 1.2,
                            "text": "very",
                            "words": [{"word": "very", "start": 1.0, "end": 1.2}],
                        },
                        {
                            "start": 1.2,
                            "end": 1.4,
                            "text": "very",
                            "words": [{"word": "very", "start": 1.2, "end": 1.4}],
                        },
                    ]
                }
            ]
        )

        self.assertEqual(
            [segment["text"] for segment in merged["segments"]],
            ["very", "very"],
        )


def test_normalized_chunk_preserves_samples_and_amplifies_quiet_audio(tmp_path):
    import math
    import struct
    import wave
    from app.core.bk_asr.mlx_workflow import extract_audio_chunk

    rate = 16000
    source = tmp_path / 'quiet.wav'
    samples = [int(500 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(rate * 4)]
    with wave.open(str(source), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack(f'<{len(samples)}h', *samples))
    plain = extract_audio_chunk(source, tmp_path / 'plain.wav', 0, 4)
    normalized = extract_audio_chunk(source, tmp_path / 'normalized.wav', 0, 4, normalize_audio=True)
    def read(path):
        with wave.open(str(path), 'rb') as handle:
            count = handle.getnframes()
            assert handle.getframerate() == rate
            return count, struct.unpack(f'<{count}h', handle.readframes(count))
    plain_count, plain_samples = read(plain)
    normalized_count, normalized_samples = read(normalized)
    assert plain_count == normalized_count == rate * 4
    assert sum(s*s for s in normalized_samples) > 4 * sum(s*s for s in plain_samples)
    assert max(abs(s) for s in normalized_samples) < 32767
