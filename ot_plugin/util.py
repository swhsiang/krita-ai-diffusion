from enum import Enum, Flag
from dataclasses import asdict, is_dataclass
from itertools import islice
from pathlib import Path
from typing import Generator
import asyncio
import importlib.util
import os
import subprocess
import sys
import json
import logging
import logging.handlers
import statistics
import zipfile
from typing import Any, Callable, Iterable, Optional, Sequence, TypeVar
from PyQt5 import sip
from PyQt5.QtCore import QObject, QStandardPaths, QByteArray
from PyQt5.QtGui import QImage
from collections import OrderedDict
import time
# import numpy as np
T = TypeVar("T")
R = TypeVar("R")
QOBJECT = TypeVar("QOBJECT", bound=QObject)

is_windows = sys.platform.startswith("win")
is_macos = sys.platform == "darwin"
is_linux = not is_windows and not is_macos

plugin_dir = dir = Path(__file__).parent


def _get_user_data_dir():
    if importlib.util.find_spec("krita") is None:
        dir = plugin_dir.parent / ".appdata"
        dir.mkdir(exist_ok=True)
        return dir
    try:
        dir = Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation))
        if dir.exists() and "krita" in dir.name.lower():
            dir = dir / "ai_diffusion"
        else:
            dir = Path(QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation))
            dir = dir / "krita-ai-diffusion"
        dir.mkdir(exist_ok=True)
        return dir
    except Exception as e:
        return Path(__file__).parent


user_data_dir = _get_user_data_dir()


def _get_log_dir():
    dir = user_data_dir / "logs"
    dir.mkdir(exist_ok=True)

    legacy_dir = plugin_dir / ".logs"
    try:  # Move logs from old location (v1.14 and earlier)
        if legacy_dir.exists():
            for file in legacy_dir.iterdir():
                file.rename(dir / file.name)
            legacy_dir.rmdir()
    except Exception:
        print(f"Failed to move logs from {legacy_dir} to {dir}")

    return dir


def create_logger(name: str, path: Path):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if os.environ.get("AI_DIFFUSION_ENV") == "WORKER":
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.handlers.RotatingFileHandler(
            path, encoding="utf-8", maxBytes=10 * 1024 * 1024, backupCount=4
        )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


log_dir = _get_log_dir()
client_logger = create_logger("krita.ai_diffusion.client", log_dir / "client.log")
server_logger = create_logger("krita.ai_diffusion.server", log_dir / "server.log")
ot_client_logger = create_logger("krita.ai_diffusion.ot_client", log_dir / "ot_client.log")

def _log_error(logger: logging.Logger, error: Exception):
    message = str(error)
    if isinstance(error, AssertionError):
        message = f"Error: Internal assertion failed [{error}]"
    elif not message.startswith("Error:"):
        message = f"Error: {message}"
    logger.exception(message)
    return message


def ot_log_error(error: Exception):
    return _log_error(ot_client_logger, error)


def log_error(error: Exception):
    return _log_error(client_logger, error)


def ensure(value: Optional[T], msg="") -> T:
    assert value is not None, msg or "a value is required"
    return value


def maybe(func: Callable[[T], R], value: Optional[T]) -> Optional[R]:
    if value is not None:
        return func(value)
    return None


def batched(iterable, n):
    it = iter(iterable)
    while batch := tuple(islice(it, n)):
        yield batch


def clamp(value: int, min_value: int, max_value: int):
    return max(min(value, max_value), min_value)


def median_or_zero(values: Iterable[float]) -> float:
    try:
        return statistics.median(values)
    except statistics.StatisticsError:
        return 0


def isnumber(x):
    return isinstance(x, (int, float))


def base_type_match(a, b):
    return type(a) == type(b) or (isnumber(a) and isnumber(b))


def unique(seq: Sequence[T], key) -> list[T]:
    seen = set()
    return [x for x in seq if (k := key(x)) not in seen and not seen.add(k)]


def flatten(seq: Sequence[T | list[T]]) -> Generator[T, None, None]:
    for x in seq:
        if isinstance(x, list):
            yield from x
        else:
            yield x


def trim_text(text: str, max_length: int) -> str:
    if len(text) > max_length:
        return text[: max_length - 3] + "..."
    return text


def encode_json(obj: Any):
    if isinstance(obj, Flag):
        return obj.value
    if isinstance(obj, Enum):
        return obj.name
    if isinstance(obj, Path):
        return str(obj.as_posix())
    if is_dataclass(obj):
        assert not isinstance(obj, type)
        return asdict(obj)
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


def read_json_with_comments(path: Path):
    lines = path.read_text().splitlines()
    return json.loads("\n".join("" if line.strip().startswith("//") else line for line in lines))


def sanitize_prompt(prompt: str):
    if prompt == "":
        return "no prompt"
    prompt = prompt[:40]
    return "".join(c for c in prompt if c.isalnum() or c in " _-")


def find_unused_path(path: Path):
    """Finds an unused path by appending a number to the filename"""
    if not path.exists():
        return path
    stem = path.stem
    ext = path.suffix
    i = 1
    while (new_path := path.with_name(f"{stem}-{i}{ext}")).exists():
        i += 1
    return new_path


if is_linux:
    import signal
    import ctypes

    libc = ctypes.CDLL("libc.so.6")

    def set_pdeathsig():
        return libc.prctl(1, signal.SIGTERM)


async def create_process(
    program: str | Path,
    *args: str,
    cwd: Path | None = None,
    additional_env: dict | None = None,
    pipe_stderr=False,
):
    platform_args = {}
    if is_windows:
        platform_args["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore
    if is_linux:
        platform_args["preexec_fn"] = set_pdeathsig

    env = os.environ.copy()
    if additional_env:
        env.update(additional_env)
    if "PYTHONPATH" in env:
        del env["PYTHONPATH"]  # Krita adds its own python path, which can cause conflicts

    out = asyncio.subprocess.PIPE
    err = asyncio.subprocess.PIPE if pipe_stderr else asyncio.subprocess.STDOUT

    p = await asyncio.create_subprocess_exec(
        program, *args, cwd=cwd, stdout=out, stderr=err, env=env, **platform_args
    )
    if is_windows:
        try:
            from . import win32

            win32.attach_process_to_job(p.pid)
        except Exception as e:
            client_logger.error(f"Failed to attach process to job: {e}")
    return p


class LongPathZipFile(zipfile.ZipFile):
    # zipfile.ZipFile does not support long paths (260+?) on Windows
    # for latest python, changing cwd and using relative paths helps, but not for python in Krita 5.2
    def _extract_member(self, member, targetpath, pwd):
        # Prepend \\?\ to targetpath to bypass MAX_PATH limit
        targetpath = os.path.abspath(targetpath)
        if targetpath.startswith("\\\\"):
            targetpath = "\\\\?\\UNC\\" + targetpath[2:]
        else:
            targetpath = "\\\\?\\" + targetpath
        return super()._extract_member(member, targetpath, pwd)  # type: ignore


ZipFile = LongPathZipFile if is_windows else zipfile.ZipFile


def acquire_elements(l: list[QOBJECT]) -> list[QOBJECT]:
    # Many Pykrita functions return a `QList<QObject*>` where the objects are
    # allocated for the caller. SIP does not handle this case and just leaks
    # the objects outright. Fix this by taking explicit ownership of the objects.
    # Note: ONLY call this if you are confident that the Pykrita function
    # allocates the list members!
    for obj in l:
        if obj is not None:
            sip.transferback(obj)
    return l


class LRUCacheWithTTL:
    def __init__(self, capacity: int, ttl: int):
        self.cache = OrderedDict()
        self.capacity = capacity
        self.ttl = ttl
        self.lock = asyncio.Lock()

    def _is_expired(self, key):
        return time.time() - self.cache[key][1] > self.ttl

    async def get(self, key: Any) -> Any:
        async with self.lock:
            if key in self.cache:
                if self._is_expired(key):
                    del self.cache[key]
                    return None
                self.cache.move_to_end(key)
                return self.cache[key][0]
            return None

    async def set(self, key: Any, value: Any):
        async with self.lock:
            if key in self.cache:
                if self._is_expired(key):
                    del self.cache[key]
                else:
                    self.cache.move_to_end(key)
            elif len(self.cache) >= self.capacity:
                self.cache.popitem(last=False)
            self.cache[key] = (value, time.time())

    async def get_contents_string(self) -> str:
        """Get a thread-safe string representation of the cache contents."""
        async with self.lock:
            return str({k: v[0] for k, v in self.cache.items() if not self._is_expired(k)})

    def __str__(self):
        import warnings
        warnings.warn("Using non-thread-safe __str__. For thread-safe access, use get_contents_string()")
        return str({k: v[0] for k, v in self.cache.items() if not self._is_expired(k)})

    async def keys(self) -> list:
        """Get a list of non-expired keys in the cache in a thread-safe manner."""
        async with self.lock:
            return [k for k in self.cache.keys() if not self._is_expired(k)]

    async def values(self) -> list:
        """Get a list of values for non-expired entries in the cache in a thread-safe manner."""
        async with self.lock:
            return [v[0] for k, v in self.cache.items() if not self._is_expired(k)]

    async def items(self) -> list:
        """Get a list of (key, value) pairs for non-expired entries in the cache in a thread-safe manner."""
        async with self.lock:
            return [(k, v[0]) for k, v in self.cache.items() if not self._is_expired(k)]

def qimage_to_numpy(image: QImage):
    """Convert a QImage to a NumPy array."""
    # width = image.width()
    # height = image.height()
    # ptr = image.bits()
    # ptr.setsize(image.byteCount())
    # arr = np.array(ptr).reshape(height, width, 4)  # Assuming ARGB32 format
    # return arr
    pass

def compare_images_np(image1: QImage, image2: QImage):
    """Compare two QImages and return the differences as a NumPy array."""
    # arr1 = qimage_to_numpy(image1)
    # arr2 = qimage_to_numpy(image2)
    
    # if arr1.shape != arr2.shape:
    #     raise ValueError("Images must have the same dimensions to compare.")
    
    # # Compute the difference
    # diff = np.abs(arr1 - arr2)
    
    # return diff
    pass

def qbytearray_to_numpy(byte_array: QByteArray, width: int, height: int, channels: int = 4):
    """Convert a QByteArray to a NumPy array.
    
    Args:
        byte_array (QByteArray): The input QByteArray containing raw pixel data.
        width (int): The width of the image.
        height (int): The height of the image.
        channels (int): The number of color channels (default is 4 for ARGB32 format).
        
    Returns:
        np.ndarray: The resulting NumPy array.
    """
    # # Convert QByteArray to bytes
    # byte_data = byte_array.data()
    
    # # Create a NumPy array from the byte data
    # np_array = np.frombuffer(byte_data, dtype=np.uint8)
    
    # # Reshape the array to the desired dimensions
    # np_array = np_array.reshape((height, width, channels))
    
    # return np_array
    pass

def numpy_to_qbytearray(np_array) -> QByteArray:
    """Convert a NumPy array to a QByteArray.
    
    Args:
        np_array (np.ndarray): The input NumPy array.
        
    Returns:
        QByteArray: The resulting QByteArray.
    """
    # # Ensure the NumPy array is in the correct format
    # if np_array.dtype != np.uint8:
    #     raise ValueError("NumPy array must have dtype of uint8")
    
    # # Convert the NumPy array to bytes
    # byte_data = np_array.tobytes()
    
    # # Create a QByteArray from the byte data
    # byte_array = QByteArray(byte_data)
    
    # return byte_array
    pass

def compare_qbytearray(byte_array1: QByteArray, byte_array2: QByteArray):
    raise NotImplementedError
    # arr1 = qbytearray_to_numpy(byte_array1)
    # arr2 = qbytearray_to_numpy(byte_array2)
    # if arr1.shape != arr2.shape:
    #     raise ValueError("QByteArrays must have the same dimensions to compare.")
    # return np.abs(arr1 - arr2)
    pass
