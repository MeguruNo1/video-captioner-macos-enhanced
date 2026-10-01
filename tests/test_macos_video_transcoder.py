import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

import pytest

from app.core.utils import macos_video_transcoder


class _FakeTrack:
    def __init__(self, descriptions):
        self._descriptions = descriptions

    def formatDescriptions(self):
        return self._descriptions


class _FakeAsset:
    def __init__(self, tracks):
        self._tracks = tracks

    def tracksWithMediaType_(self, _media_type):
        return self._tracks

    def isReadable(self):
        return bool(self._tracks)

    def isExportable(self):
        return bool(self._tracks)


class _FakeSession:
    def __init__(self, status, file_type, output_bytes=b"hevc"):
        self._status = status
        self._file_type = file_type
        self._output_bytes = output_bytes
        self._output_url = None

    def supportedFileTypes(self):
        return [self._file_type]

    def setOutputURL_(self, value):
        self._output_url = value

    def setOutputFileType_(self, _value):
        pass

    def exportAsynchronouslyWithCompletionHandler_(self, handler):
        assert self._output_url is not None
        Path(self._output_url).write_bytes(self._output_bytes)
        handler()

    def progress(self):
        return 1.0

    def status(self):
        return self._status

    def error(self):
        return None


class _FakeExportSessionFactory:
    def __init__(self, session):
        self._session = session
        self.preset = None

    def alloc(self):
        return self

    def initWithAsset_presetName_(self, _asset, _preset):
        self.preset = _preset
        return self._session


class MacOSVideoTranscoderTests(unittest.TestCase):
    def test_get_native_video_codec_maps_av1_fourcc(self):
        av = SimpleNamespace(AVMediaTypeVideo="video")
        cm = SimpleNamespace(CMFormatDescriptionGetMediaSubType=lambda _description: int.from_bytes(b"av01", "big"))
        foundation = SimpleNamespace()

        with patch.object(macos_video_transcoder.sys, "platform", "darwin"), patch.object(
            macos_video_transcoder, "_load_frameworks", return_value=(av, cm, foundation)
        ), patch.object(
            macos_video_transcoder,
            "_asset_for_path",
            return_value=_FakeAsset([_FakeTrack(["description"])]),
        ):
            self.assertEqual(macos_video_transcoder.get_native_video_codec("input.mp4"), "av1")

    def test_get_native_video_codec_maps_vp9_fourcc(self):
        av = SimpleNamespace(AVMediaTypeVideo="video")
        cm = SimpleNamespace(CMFormatDescriptionGetMediaSubType=lambda _description: int.from_bytes(b"vp09", "big"))
        foundation = SimpleNamespace()

        with patch.object(macos_video_transcoder.sys, "platform", "darwin"), patch.object(
            macos_video_transcoder, "_load_frameworks", return_value=(av, cm, foundation)
        ), patch.object(
            macos_video_transcoder,
            "_asset_for_path",
            return_value=_FakeAsset([_FakeTrack(["description"])]),
        ):
            self.assertEqual(macos_video_transcoder.get_native_video_codec("input.mp4"), "vp9")

    def test_transcode_video_to_hevc_native_exports_with_avfoundation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.mp4"
            output_path = Path(temp_dir) / "output.mp4"
            input_path.write_bytes(b"input")
            progress_events = []

            av = SimpleNamespace(
                AVMediaTypeVideo="video",
                AVAssetExportPresetHEVCHighestQuality="HEVC_HQ",
                AVFileTypeMPEG4="public.mpeg-4",
                AVAssetExportSessionStatusCompleted=3,
            )
            av.AVAssetExportSession = _FakeExportSessionFactory(
                _FakeSession(av.AVAssetExportSessionStatusCompleted, av.AVFileTypeMPEG4)
            )
            foundation = SimpleNamespace(NSURL=SimpleNamespace(fileURLWithPath_=lambda value: value))

            with patch.object(macos_video_transcoder.sys, "platform", "darwin"), patch.object(
                macos_video_transcoder, "_load_frameworks", return_value=(av, SimpleNamespace(), foundation)
            ), patch.object(
                macos_video_transcoder, "_asset_for_path", return_value=_FakeAsset([_FakeTrack([])])
            ):
                result = macos_video_transcoder.transcode_video_to_hevc_native(
                    str(input_path),
                    str(output_path),
                    progress_callback=lambda value, text: progress_events.append((value, text)),
                )

            self.assertEqual(result, "macos_avfoundation_hevc")
            self.assertTrue(output_path.is_file())
            self.assertIn((100, "H.265 转码完成"), progress_events)

    def test_transcode_video_to_hevc_native_uses_selected_4k_preset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.mp4"
            output_path = Path(temp_dir) / "output.mp4"
            input_path.write_bytes(b"input")

            av = SimpleNamespace(
                AVMediaTypeVideo="video",
                AVAssetExportPresetHEVCHighestQuality="HEVC_HQ",
                AVAssetExportPresetHEVC3840x2160="HEVC_4K",
                AVFileTypeMPEG4="public.mpeg-4",
                AVAssetExportSessionStatusCompleted=3,
            )
            factory = _FakeExportSessionFactory(
                _FakeSession(av.AVAssetExportSessionStatusCompleted, av.AVFileTypeMPEG4)
            )
            av.AVAssetExportSession = factory
            foundation = SimpleNamespace(
                NSURL=SimpleNamespace(fileURLWithPath_=lambda value: value)
            )

            with patch.object(
                macos_video_transcoder.sys, "platform", "darwin"
            ), patch.object(
                macos_video_transcoder,
                "_load_frameworks",
                return_value=(av, SimpleNamespace(), foundation),
            ), patch.object(
                macos_video_transcoder, "_asset_for_path", return_value=_FakeAsset([_FakeTrack([])])
            ):
                macos_video_transcoder.transcode_video_to_hevc_native(
                    str(input_path),
                    str(output_path),
                    preset_name=macos_video_transcoder.NATIVE_HEVC_PRESET_BALANCED_4K,
                )

            self.assertEqual(factory.preset, "HEVC_4K")


if __name__ == "__main__":
    unittest.main()


def test_unreadable_asset_is_rejected_before_deleting_output(tmp_path):
    source, target = tmp_path / "source.webm", tmp_path / "target.mp4"
    source.write_bytes(b"source")
    target.write_bytes(b"existing")
    av = SimpleNamespace(AVMediaTypeVideo="video")
    with (patch.object(macos_video_transcoder, "is_native_hevc_transcode_supported", return_value=True),
          patch.object(macos_video_transcoder, "_load_frameworks", return_value=(av, None, None)),
          patch.object(macos_video_transcoder, "_asset_for_path", return_value=_FakeAsset([]))):
        with pytest.raises(RuntimeError, match=r"readable=False.*video_tracks=0"):
            macos_video_transcoder.transcode_video_to_hevc_native(str(source), str(target))
    assert target.read_bytes() == b"existing"
    assert source.read_bytes() == b"source"


def test_native_error_preserves_domain_code_and_underlying_reason():
    inner = Mock()
    inner.domain.return_value = "NSOSStatusErrorDomain"
    inner.code.return_value = -16979
    inner.localizedDescription.return_value = "Cannot read media"
    inner.localizedFailureReason.return_value = None
    inner.userInfo.return_value = {}
    outer = Mock()
    outer.domain.return_value = "AVFoundationErrorDomain"
    outer.code.return_value = -11800
    outer.localizedDescription.return_value = "Operation failed"
    outer.localizedFailureReason.return_value = "Underlying error"
    outer.userInfo.return_value = {"NSUnderlyingError": inner}
    session = Mock()
    session.error.return_value = outer
    text = macos_video_transcoder._session_error_text(session)
    assert "AVFoundationErrorDomain (-11800)" in text
    assert "NSOSStatusErrorDomain (-16979)" in text


def test_cancellation_stops_native_export(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"input")
    session = Mock()
    session.supportedFileTypes.return_value = ["mp4"]
    av = SimpleNamespace(AVMediaTypeVideo="video", AVAssetExportPresetHEVCHighestQuality="HQ",
                         AVFileTypeMPEG4="mp4", AVAssetExportSession=_FakeExportSessionFactory(session))
    foundation = SimpleNamespace(NSURL=SimpleNamespace(fileURLWithPath_=lambda value: value))
    cancel = Mock(side_effect=[None, RuntimeError("cancelled")])
    with (patch.object(macos_video_transcoder, "is_native_hevc_transcode_supported", return_value=True),
          patch.object(macos_video_transcoder, "_load_frameworks", return_value=(av, None, foundation)),
          patch.object(macos_video_transcoder, "_asset_for_path", return_value=_FakeAsset([_FakeTrack([])]))):
        with pytest.raises(RuntimeError, match="cancelled"):
            macos_video_transcoder.transcode_video_to_hevc_native(str(source), str(tmp_path / "out.mp4"), cancel_check=cancel)
    session.cancelExport.assert_called_once()
