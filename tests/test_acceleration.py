import subprocess
import sys
from unittest.mock import patch

import pytest

from app.core.utils.acceleration import select_asr, resolve_whisperx_device
from app.mcp.jobs import JobManager, check_environment
from app.mcp.worker import Worker


@pytest.fixture
def hardware():
    return {"mlx": {"available": False}, "cuda": {"available": True, "compute_types": ["float16", "int8_float16", "float32"]},
            "cpu_compute_types": ["int8", "float32"], "warnings": []}


def test_auto_prefers_metal_on_apple_then_cuda_then_cpu(hardware):
    hardware['mlx']['available'] = True
    assert select_asr(hardware=hardware) == {"backend": "mlx", "device": "metal", "compute_type": "model"}
    hardware['mlx']['available'] = False
    assert select_asr(hardware=hardware)['device'] == 'cuda'
    hardware['cuda']['available'] = False
    assert select_asr(hardware=hardware) == {"backend": "whisperx", "device": "cpu", "compute_type": "int8"}


def test_explicit_cpu_honored_even_on_metal_or_cuda(hardware):
    hardware['mlx']['available'] = True
    assert select_asr(device='cpu', hardware=hardware)['device'] == 'cpu'


@pytest.mark.parametrize('device', ['mps', 'directml', 'rocm', 'gpu'])
def test_unsupported_whisperx_devices_rejected(hardware, device):
    with pytest.raises(ValueError):
        resolve_whisperx_device(device, hardware=hardware)


def test_explicit_cuda_does_not_silently_fallback(hardware):
    hardware['cuda'] = {"available": False, "reason": "CPU-only PyTorch"}
    with pytest.raises(ValueError, match='CPU-only PyTorch'):
        select_asr(backend='whisperx', device='cuda', hardware=hardware)


def test_precision_matches_runtime_not_assumed(hardware):
    hardware['cuda']['compute_types'] = ['float32']
    assert resolve_whisperx_device('auto', 'auto', hardware)['compute_type'] == 'float32'
    with pytest.raises(ValueError, match='does not support'):
        resolve_whisperx_device('cpu', 'float16', hardware)
    assert resolve_whisperx_device('cuda', 'float32', hardware)['compute_type'] == 'float32'


def test_mlx_request_is_not_replaced_by_whisperx(hardware):
    with pytest.raises(ValueError, match='MLX Metal'):
        select_asr('mlx', hardware=hardware)


def test_environment_checks_only_selected_backend(tmp_path, hardware, monkeypatch):
    monkeypatch.setattr('app.core.utils.acceleration.inspect_acceleration', lambda: hardware)
    monkeypatch.setattr('app.mcp.jobs.shutil.which', lambda name: '/bin/' + name)
    real_find = __import__('importlib.util', fromlist=['find_spec']).find_spec
    monkeypatch.setattr('app.mcp.jobs.importlib.util.find_spec', lambda name: None if name == 'mlx_whisper' else real_find(name))
    model = tmp_path / 'model'
    model.mkdir()
    for name in ['model.bin', 'config.json', 'tokenizer.json']:
        (model / name).touch()
    result = check_environment(str(model), backend='whisperx', device='cuda')
    assert result['ready'], result['errors']
    assert 'mlx_whisper' not in result['packages']
    assert result['selection']['compute_type'] == 'float16'
    (model / 'model.bin').unlink()
    assert not check_environment(str(model), backend='whisperx')['ready']


def test_mcp_snapshots_cuda_and_backend_settings(tmp_path, monkeypatch):
    selected = {'backend': 'whisperx', 'device': 'cuda', 'compute_type': 'float16'}
    monkeypatch.setattr('app.mcp.jobs.check_environment', lambda *a: {'ready': True, 'errors': [], 'local_model': '/local/model', 'selection': selected})
    monkeypatch.setattr('app.mcp.jobs.read_settings', lambda: {'WhisperX': {'BatchSize': 4, 'Hotwords': 'Ada', 'InitialPrompt': 'hello'}})
    manager = JobManager(tmp_path / 'registry')
    monkeypatch.setattr(manager, 'resume_job', lambda job_id: manager.get_job(job_id))
    result = manager.start_job('https://example.com/video', output_dir=str(tmp_path / 'output'), proxy_url='', backend='whisperx', device='cuda')
    options = manager.store.read(result['job_id'])['options']
    assert options['device'] == 'cuda'
    assert options['compute_type'] == 'float16'
    assert options['batch_size'] == 4
    assert options['mlx_hotwords'] == 'Ada'
    assert options['initial_prompt'] == 'hello'
    assert result['workflow_settings']['asr']['backend'] == 'whisperx'


def test_worker_whisperx_route_preserves_device_and_word_alignment(tmp_path):
    worker = Worker(tmp_path, 'a' * 32, 'token')
    with patch('app.core.bk_asr.whisper_x_auto.WhisperXASR') as cls:
        result = {'segments': [{'words': [{'word': 'Hi', 'start': 0, 'end': 1}]}]}
        cls.return_value._run.return_value = result
        options = {'backend': 'whisperx', 'device': 'cuda', 'compute_type': 'float16',
                   'model': '/model', 'source_language': 'auto', 'initial_prompt': '', 'batch_size': 2}
        assert worker.transcribe(tmp_path / 'audio.wav', options) == result
        kwargs = cls.call_args.kwargs
        assert kwargs['device'] == 'cuda' and kwargs['compute_type'] == 'float16'
        assert kwargs['align'] and kwargs['need_word_time_stamp']
        assert kwargs['language'] is None and kwargs['batch_size'] == 2


def test_legacy_worker_jobs_still_use_mlx_native_timestamps(tmp_path):
    worker = Worker(tmp_path, 'a' * 32, 'token')
    with patch('app.core.bk_asr.mlx_whisper.MLXWhisperASR') as cls:
        worker.transcribe(tmp_path / 'audio.wav', {'model': '/model', 'source_language': 'en', 'initial_prompt': '', 'vad_enabled': False})
        assert cls.call_args.kwargs['alignment_method'] == 'native'


def test_desktop_transcribe_does_not_overwrite_cuda(tmp_path):
    from app.core.entities import TranscribeConfig, TranscribeModelEnum
    from app.core.bk_asr.transcribe import transcribe
    config = TranscribeConfig(transcribe_model=TranscribeModelEnum.WHISPER_X, whisperx_device='cuda', whisperx_compute_type='float16')
    with patch('app.core.bk_asr.whisper_x_auto.WhisperXASR') as cls:
        transcribe(str(tmp_path / 'audio.wav'), config)
        assert cls.call_args.kwargs['device'] == 'cuda'
        assert cls.call_args.kwargs['compute_type'] == 'float16'


def test_whisperx_import_remains_headless():
    code = "from app.core.bk_asr.whisper_x_auto import WhisperXASR; import sys; assert not any(n.startswith('PyQt5') or n == 'openai' for n in sys.modules)"
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cross_process_lock_excludes_second_worker(tmp_path):
    from app.mcp.store import file_lock
    lock = tmp_path / 'task.lock'
    code = '''
import sys
from pathlib import Path
from app.mcp.store import file_lock
try:
    with file_lock(Path(sys.argv[1]), blocking=False):
        pass
except OSError:
    sys.exit(42)
'''
    with file_lock(lock):
        result = subprocess.run([sys.executable, '-c', code, str(lock)], capture_output=True, timeout=10)
        assert result.returncode == 42
    result = subprocess.run([sys.executable, '-c', code, str(lock)], capture_output=True, timeout=10)
    assert result.returncode == 0


def test_task_factory_preserves_user_cuda_selection(tmp_path, monkeypatch):
    from app.common.config import cfg
    from app.core.task_factory import TaskFactory
    from app.core.entities import TranscribeModelEnum
    monkeypatch.setattr(cfg.transcribe_model, 'value', TranscribeModelEnum.WHISPER_X)
    monkeypatch.setattr(cfg.whisperx_device, 'value', 'cuda')
    monkeypatch.setattr(cfg.whisperx_compute_type, 'value', 'float16')
    task = TaskFactory.create_transcribe_task(str(tmp_path / 'audio.wav'))
    assert task.transcribe_config.whisperx_device == 'cuda'
    assert task.transcribe_config.whisperx_compute_type == 'float16'


def test_backend_resolves_auto_before_loading_model(tmp_path, hardware):
    from app.core.bk_asr.whisper_x_auto import WhisperXASR
    audio = tmp_path / 'audio.wav'
    audio.write_bytes(b'RIFF')
    asr = WhisperXASR(str(audio), device='auto', compute_type='auto')
    with patch.dict(sys.modules, {'whisperx': __import__('types').SimpleNamespace()}), \
         patch('app.core.utils.acceleration.inspect_acceleration', return_value=hardware), \
         patch.object(asr, '_run_local', return_value={'segments': []}) as run:
        asr._run()
        assert asr.device == 'cuda' and asr.compute_type == 'float16'
        run.assert_called_once()


def test_windows_byte_lock_uses_same_offset_for_lock_and_unlock(tmp_path):
    import os
    from types import SimpleNamespace
    from app.mcp import store
    calls = []
    def locking(fd, operation, length):
        calls.append((os.lseek(fd, 0, os.SEEK_CUR), operation, length))
    fake = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
    with patch.object(store, 'os', SimpleNamespace(name='nt')), patch.object(store, 'msvcrt', fake, create=True):
        with store.file_lock(tmp_path / 'byte.lock'):
            pass
    assert calls == [(0, 2, 1), (0, 0, 1)]


def test_windows_worker_uses_stable_log_outside_movable_task_directory(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app.mcp import jobs
    manager = JobManager(tmp_path / 'registry')
    state = {'job_id': 'a' * 32, 'directory': str(tmp_path / 'staging')}
    process = Mock(pid=123)
    popen = Mock(return_value=process)
    subprocess_stub = SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL,
                                      CREATE_NEW_PROCESS_GROUP=512, DETACHED_PROCESS=8)
    with patch.object(jobs, 'os', SimpleNamespace(name='nt')), \
         patch.object(jobs, 'subprocess', subprocess_stub), \
         patch.object(jobs.psutil, 'Process', return_value=Mock(create_time=lambda: 123.0)), \
         patch.object(jobs.threading, 'Thread', return_value=Mock()):
        manager._spawn(state)
    assert state['worker_log_path'] == str(tmp_path / 'registry/logs' / ('a' * 32 + '.log'))
    assert not (tmp_path / 'staging/worker.log').exists()
    assert popen.call_args.kwargs['creationflags'] == 520
    assert 'start_new_session' not in popen.call_args.kwargs
