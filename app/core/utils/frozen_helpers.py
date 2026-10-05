"""Route known joblib child processes before loading the desktop application."""
import re
import runpy
import sys


def _run_tracker(fd: int, verbose: int) -> None:
    from joblib.externals.loky.backend.resource_tracker import main
    main(fd, verbose)


def dispatch_frozen_helper(arguments: list[str]) -> bool:
    if "-c" in arguments:
        index = arguments.index("-c")
        if len(arguments) != index + 2:
            return False
        match = re.fullmatch(
            r"from joblib\.externals\.loky\.backend\.resource_tracker import main; "
            r"main\((\d+), (True|False|\d+)\)", arguments[index + 1])
        if match is None:
            return False
        verbosity = {"True": 1, "False": 0}.get(match[2])
        _run_tracker(int(match[1]), int(match[2]) if verbosity is None else verbosity)
        return True
    if len(arguments) >= 2 and arguments[:2] == ["-m", "joblib.externals.loky.backend.popen_loky_posix"]:
        sys.argv = [arguments[1], *arguments[2:]]
        runpy.run_module(arguments[1], run_name="__main__")
        return True
    return False
