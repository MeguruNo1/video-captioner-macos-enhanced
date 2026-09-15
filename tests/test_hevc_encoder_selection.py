from unittest.mock import patch
import pytest
from app.core.utils import video_utils
from app.core.download_service import VideoDownloadService
from app.mcp.settings import workflow_settings_snapshot


@pytest.mark.parametrize('encoder', ['hevc_nvenc', 'hevc_qsv', 'hevc_amf', 'libx265'])
def test_explicit_encoder_is_used_without_fallback(tmp_path, encoder):
    source = tmp_path / 'input.mp4'
    source.touch()
    with patch.object(video_utils, '_get_available_ffmpeg_encoders', return_value={encoder, 'libx265'}), \
         patch.object(video_utils, '_run_hevc_transcode_command') as run:
        result = video_utils.transcode_video_to_hevc(str(source), str(tmp_path / 'output.mp4'), encoder_preference=encoder)
        assert result == encoder
        assert run.call_args.args[0][run.call_args.args[0].index('-c:v') + 1] == encoder
        run.assert_called_once()
        run.reset_mock()
        run.side_effect = RuntimeError('device unavailable')
        with pytest.raises(RuntimeError, match='device unavailable'):
            video_utils.transcode_video_to_hevc(str(source), str(tmp_path / 'output.mp4'), encoder_preference=encoder)
        run.assert_called_once()


def test_missing_explicit_encoder_reports_error(tmp_path):
    source = tmp_path / 'input.mp4'
    source.touch()
    with patch.object(video_utils, '_get_available_ffmpeg_encoders', return_value={'libx265'}), \
         patch.object(video_utils, '_run_hevc_transcode_command') as run:
        with pytest.raises(RuntimeError, match='hevc_nvenc'):
            video_utils.transcode_video_to_hevc(str(source), str(tmp_path / 'output.mp4'), encoder_preference='hevc_nvenc')
        run.assert_not_called()


def test_explicit_cpu_bypasses_apple_native_transcode(tmp_path):
    service = VideoDownloadService('https://example.com/video', str(tmp_path), hevc_encoder='libx265', pr_smart_transcode_hevc_on_av1=True)
    with patch('app.core.download_service.is_native_hevc_transcode_supported', return_value=True), \
         patch('app.core.download_service.get_video_codec', return_value='av1'), \
         patch('app.core.download_service.transcode_video_to_hevc_native') as native, \
         patch('app.core.download_service.transcode_video_to_hevc', return_value='libx265') as ffmpeg:
        result = service._postprocess_pr_smart_hevc(str(tmp_path / 'input.mp4'))
        assert result[1] == 'libx265'
        assert ffmpeg.call_args.kwargs['encoder_preference'] == 'libx265'
        native.assert_not_called()


def test_mcp_reads_encoder_and_old_settings_default_to_auto():
    assert workflow_settings_snapshot({'Download': {'HevcEncoder': 'hevc_qsv'}})['download']['hevc_encoder'] == 'hevc_qsv'
    assert workflow_settings_snapshot({'Download': {'NativeHevcPreset': 'balanced_4k'}})['download']['hevc_encoder'] == 'auto'
