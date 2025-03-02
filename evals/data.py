"""
This file defines utilities for working with data and files of various types.
"""
import csv
import dataclasses
import gzip
import itertools
import json
import logging
import os
import urllib
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any, List, Optional, Sequence, Text, Union

import blobfile as bf
import lz4.frame
import pydantic
import pyzstd

logger = logging.getLogger(__name__)


def gzip_open(filename: str, mode: str = "rb", openhook: Any = open) -> gzip.GzipFile:
    """Wrap the given openhook in gzip."""
    if mode and "b" not in mode:
        mode += "b"

    return gzip.GzipFile(fileobj=openhook(filename, mode), mode=mode)


def lz4_open(filename: str, mode: str = "rb", openhook: Any = open) -> lz4.frame.LZ4FrameFile:
    if mode and "b" not in mode:
        mode += "b"

    return lz4.frame.LZ4FrameFile(openhook(filename, mode), mode=mode)


def zstd_open(filename: str, mode: str = "rb", openhook: Any = open) -> pyzstd.ZstdFile:
    if mode and "b" not in mode:
        mode += "b"

    return pyzstd.ZstdFile(openhook(filename, mode), mode=mode)


def open_by_file_pattern(filename: Union[Path, str], mode: str = "r", **kwargs: Any) -> Any:
    """Can read/write to files on gcs/local with or without gzipping. If file
    is stored on gcs, streams with blobfile. Otherwise use vanilla python open. If
    filename endswith gz, then zip/unzip contents on the fly (note that gcs paths and
    gzip are compatible)"""
    if type(filename) != str:
        filename = str(filename)
    open_fn = partial(bf.BlobFile, **kwargs)
    try:
        if filename.endswith(".gz"):
            return gzip_open(filename, openhook=open_fn, mode=mode)
        elif filename.endswith(".lz4"):
            return lz4_open(filename, openhook=open_fn, mode=mode)
        elif filename.endswith(".zst"):
            return zstd_open(filename, openhook=open_fn, mode=mode)
        else:
            scheme = urllib.parse.urlparse(filename).scheme
            if (not os.path.exists(filename)) and (scheme == "" or scheme == "file"):
                return open_fn(
                    os.path.join(
                        os.path.dirname(os.path.abspath(__file__)),
                        "registry",
                        "data",
                        filename,
                    ),
                    mode=mode,
                )
            else:
                return open_fn(filename, mode=mode)
    except Exception as e:
        raise RuntimeError(f"Failed to open: {filename}") from e


def _decode_json(line, path: Union[Path, str], line_number):
    if type(path) != str:
        path = str(path)
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        custom_error_message = (
            f"Error parsing JSON on line {line_number}: {e.msg} at {path}:{line_number}:{e.colno}"
        )
        logger.error(custom_error_message)
        raise ValueError(custom_error_message) from None


def _get_jsonl_file(path: Union[Path, str]):
    if type(path) != str:
        path = str(path)
    logger.info(f"Fetching {path}")
    with open_by_file_pattern(path, mode="r") as f:
        return [_decode_json(line, path, i + 1) for i, line in enumerate(f)]


def _get_json_file(path: Union[Path, str]):
    if type(path) != str:
        path = str(path)
    logger.info(f"Fetching {path}")
    with open_by_file_pattern(path, mode="r") as f:
        return json.loads(f.read())


def _stream_jsonl_file(path: Union[Path, str]) -> Iterator:
    if type(path) != str:
        path = str(path)
    logger.info(f"Streaming {path}")
    with bf.BlobFile(path, "r", streaming=True) as f:
        for line in f:
            yield json.loads(line)


def get_lines(path: Union[Path, str]) -> list[dict]:
    """
    Get a list of lines from a file.
    """
    if type(path) != str:
        path = str(path)
    with open_by_file_pattern(path, mode="r") as f:
        return f.readlines()


def get_jsonl(path: Union[Path, str]) -> list[dict]:
    """
    Extract json lines from the given path.
    If the path is a directory, look in subpaths recursively.

    Return all lines from all jsonl files as a single list.
    """
    if type(path) != str:
        path = str(path)
    if bf.isdir(path):
        result = []
        for filename in bf.listdir(path):
            if filename.endswith(".jsonl"):
                result += get_jsonl(os.path.join(path, filename))
        return result
    return _get_jsonl_file(path)


def get_jsonls(paths: Sequence[Union[Path, str]], line_limit=None) -> list[dict]:
    paths = list(map(lambda x: str(x), paths))
    return list(iter_jsonls(paths, line_limit))


def get_json(path: Union[Path, str]) -> dict:
    if type(path) != str:
        path = str(path)
    if bf.isdir(path):
        raise ValueError("Path is a directory, only files are supported")
    return _get_json_file(path)


def iter_jsonls(paths: Union[Union[Path, str], list[Union[Path, str]]], line_limit=None) -> Iterator[dict]:
    """
    For each path in the input, iterate over the jsonl files in that path.
    Look in subdirectories recursively.

    Use an iterator to conserve memory.
    """
    if type(paths) == str:
        paths = [paths]
    elif type(paths) != str:
        paths = [paths]
    elif len(paths) > 0 and type(paths[0]) != str:
        paths = list(map(lambda x: str(x), paths))

    def _iter():
        for path in paths:
            if bf.isdir(path):
                for filename in bf.listdir(path):
                    if filename.endswith(".jsonl"):
                        yield from iter_jsonls([os.path.join(path, filename)])
            else:
                yield from _stream_jsonl_file(path)

    return itertools.islice(_iter(), line_limit)


def get_csv(path: Union[Path, str], fieldnames=None):
    if type(path) != str:
        path = str(path)
    with bf.BlobFile(path, "r", cache_dir="/tmp/bf_cache", streaming=False) as f:
        reader = csv.DictReader(f, fieldnames=fieldnames)
        return [row for row in reader]


def _to_py_types(o: Any, exclude_keys: List[Text]) -> Any:
    if isinstance(o, dict):
        return {
            k: _to_py_types(v, exclude_keys=exclude_keys)
            for k, v in o.items()
            if k not in exclude_keys
        }

    if isinstance(o, list):
        return [_to_py_types(v, exclude_keys=exclude_keys) for v in o]

    if isinstance(o, Path):
        return o.as_posix()

    if dataclasses.is_dataclass(o):
        return _to_py_types(dataclasses.asdict(o), exclude_keys=exclude_keys)

    # pydantic data classes
    if isinstance(o, pydantic.BaseModel):
        return {
            k: _to_py_types(v, exclude_keys=exclude_keys)
            for k, v in json.loads(o.json()).items()
            if k not in exclude_keys
        }

    return o


class EnhancedJSONEncoder(json.JSONEncoder):
    def __init__(self, exclude_keys: Optional[List[Text]] = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.exclude_keys = exclude_keys if exclude_keys else []

    def default(self, o: Any) -> str:
        return _to_py_types(o, self.exclude_keys)


def jsondumps(o: Any, ensure_ascii: bool = False, **kwargs: Any) -> str:
    # The JSONEncoder class's .default method is only applied to dictionary values,
    # not keys. In order to exclude keys from the output of this jsondumps method
    # we need to exclude them outside the encoder.
    if isinstance(o, dict) and "exclude_keys" in kwargs:
        for key in kwargs["exclude_keys"]:
            del o[key]
    return json.dumps(o, cls=EnhancedJSONEncoder, ensure_ascii=ensure_ascii, **kwargs)


def jsondump(o: Any, fp: Any, ensure_ascii: bool = False, **kwargs: Any) -> None:
    json.dump(o, fp, cls=EnhancedJSONEncoder, ensure_ascii=ensure_ascii, **kwargs)


def jsonloads(s: str, **kwargs: Any) -> Any:
    return json.loads(s, **kwargs)


def jsonload(fp: Any, **kwargs: Any) -> Any:
    return json.load(fp, **kwargs)
