"""Lazy, isolated probes and shared ASR device selection (no Qt imports)."""
import json
import os
import platform
import shutil
from pathlib import Path
import subprocess
import sys


def probe_runtime():
    """Run only in the probe process: importing GPU libraries can be slow/fatal."""
    result = {"system": platform.system(), "machine": platform.machine(),
              "cpu": platform.processor() or platform.machine(), "cpu_threads": os.cpu_count(),
              "mlx": {"available": False}, "cuda": {"available": False},
              "cpu_compute_types": [], "warnings": []}
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            inventory = subprocess.run([nvidia_smi, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                                       capture_output=True, text=True, timeout=5, check=True)
            result["nvidia_gpus"] = inventory.stdout.strip().splitlines()
        except (OSError, subprocess.SubprocessError) as exc:
            result["warnings"].append(f"NVIDIA hardware inventory failed: {exc}")
    try:
        import ctranslate2
        result["cpu_compute_types"] = sorted(ctranslate2.get_supported_compute_types("cpu"))
        result["ctranslate2_version"] = ctranslate2.__version__
    except Exception as exc:
        result["warnings"].append(f"CTranslate2: {exc}")
    try:
        import torch
        result["torch_version"] = torch.__version__
        result["torch_cuda_version"] = torch.version.cuda
        if torch.cuda.is_available():
            # Exercise the runtime, not just the presence of a GPU/driver.
            tensor = torch.ones((2, 2), device="cuda")
            (tensor @ tensor).sum().item()
            torch.cuda.synchronize()
            types = sorted(ctranslate2.get_supported_compute_types("cuda", 0))
            if not types:
                raise RuntimeError("CTranslate2 reports no CUDA compute types")
            props = torch.cuda.get_device_properties(0)
            result["cuda"] = {"available": True, "name": props.name,
                              "memory_bytes": props.total_memory, "compute_types": types}
        else:
            result["cuda"]["reason"] = "PyTorch CUDA unavailable (no compatible GPU/driver or CPU-only PyTorch)"
        result["mps_available"] = bool(torch.backends.mps.is_available())
    except Exception as exc:
        result["cuda"]["reason"] = str(exc)
    if result["system"] == "Darwin" and result["machine"].lower() in {"arm64", "aarch64"}:
        try:
            import mlx.core as mx
            if not mx.metal.is_available():
                raise RuntimeError("MLX Metal unavailable")
            with mx.stream(mx.gpu):
                mx.eval(mx.ones((2, 2)) @ mx.ones((2, 2)))
            result["mlx"] = {"available": True, "device": "metal"}
        except Exception as exc:
            result["mlx"]["reason"] = str(exc)
    else:
        result["mlx"]["reason"] = "This application's MLX backend requires Apple Silicon macOS"
    result["warnings"].append("Probe checks runtime/compute types, not model loading or available memory for your model. MPS, DirectML and ROCm are not WhisperX/CTranslate2 devices in this application.")
    return result


def inspect_acceleration(python_executable=None):
    if python_executable is None and getattr(sys, "frozen", False):
        return probe_runtime()
    try:
        process = subprocess.run([python_executable or sys.executable, str(Path(__file__).resolve())],
                                 capture_output=True, text=True, timeout=45)
        if process.returncode != 0:
            raise RuntimeError(f"Hardware probe exited {process.returncode}: {process.stderr[-1000:]}")
        return json.loads(process.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {"system": platform.system(), "machine": platform.machine(),
                "cpu": platform.processor() or platform.machine(), "cpu_threads": os.cpu_count(),
                "mlx": {"available": False}, "cuda": {"available": False, "reason": str(exc)},
                "cpu_compute_types": [], "warnings": [f"Hardware probe failed: {exc}"]}


def resolve_whisperx_device(device="auto", compute_type="auto", hardware=None):
    hardware = hardware if hardware is not None else inspect_acceleration()
    if device not in {"auto", "cpu", "cuda"}:
        raise ValueError("WhisperX device must be auto, cpu or cuda; MPS/DirectML/ROCm are not supported")
    selected = ("cuda" if hardware["cuda"]["available"] else "cpu") if device == "auto" else device
    if selected == "cuda" and not hardware["cuda"]["available"]:
        raise ValueError("CUDA is not usable: " + hardware["cuda"].get("reason", "runtime probe failed"))
    supported = hardware["cuda"].get("compute_types", []) if selected == "cuda" else hardware.get("cpu_compute_types", [])
    if not supported:
        raise ValueError(f"Cannot verify CTranslate2 {selected} support; check the Python environment. " + "; ".join(hardware.get("warnings", [])))
    if compute_type in {None, "", "auto", "default"}:
        preferences = ["float16", "int8_float16", "int8", "float32"] if selected == "cuda" else ["int8", "int8_float32", "float32"]
        compute_type = next((value for value in preferences if value in supported), supported[0])
    if compute_type not in supported:
        raise ValueError(f"{selected} does not support {compute_type}; supported: {', '.join(supported)}. Choose auto or a supported precision.")
    return {"backend": "whisperx", "device": selected, "compute_type": compute_type}


def select_asr(backend="auto", device="auto", compute_type="auto", hardware=None):
    hardware = hardware if hardware is not None else inspect_acceleration()
    if backend not in {"auto", "mlx", "whisperx"}:
        raise ValueError("backend must be auto, mlx or whisperx")
    if backend == "auto":
        backend = "mlx" if device in {"auto", "metal"} and hardware["mlx"]["available"] else "whisperx"
    if backend == "mlx":
        if device not in {"auto", "metal"} or compute_type not in {None, "auto", "model"}:
            raise ValueError("MLX uses Metal and model precision; omit device/compute_type or use auto")
        if not hardware["mlx"]["available"]:
            raise ValueError("MLX Metal is not usable: " + hardware["mlx"].get("reason", "probe failed"))
        return {"backend": "mlx", "device": "metal", "compute_type": "model"}
    return resolve_whisperx_device(device, compute_type, hardware)


if __name__ == "__main__":
    print(json.dumps(probe_runtime(), ensure_ascii=False))
