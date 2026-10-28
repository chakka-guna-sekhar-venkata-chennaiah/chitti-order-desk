"""Image references: the sandbox copy path and its guards (no real NemoClaw needed)."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from order_desk.domain.errors import ExternalServiceError, InvalidInput
from order_desk.vision.images import ImageResolver


class FakeNemohermes:
    """Acts like `nemohermes <sandbox> download <path> <dir>/`: writes a JPEG into <dir>."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if self.fail:
            return subprocess.CompletedProcess(args, 1, "", "sandbox not running")
        buf = io.BytesIO()
        Image.new("RGB", (20, 20), (9, 9, 9)).save(buf, format="JPEG")
        (Path(args[4]) / Path(args[3]).name).write_bytes(buf.getvalue())
        return subprocess.CompletedProcess(args, 0, "", "")


def _resolver(deps, runner):
    return ImageResolver(deps.db, deps.settings.upload_dir, sandbox_name="dealer-desk", runner=runner)


def test_sandbox_photo_is_copied_with_nemohermes_download(deps):
    runner = FakeNemohermes()
    image = _resolver(deps, runner).resolve("sandbox:/sandbox/.hermes/image_cache/img_abc.jpg")
    assert runner.calls[0][:4] == ["nemohermes", "dealer-desk", "download", "/sandbox/.hermes/image_cache/img_abc.jpg"]
    assert image.mime == "image/jpeg" and image.path.exists()


@pytest.mark.parametrize("path", [
    "/sandbox/.hermes/config.yaml",                       # not a media cache
    "/sandbox/.hermes/image_cache/../.env",               # path traversal
    "/etc/passwd",
])
def test_only_media_cache_paths_are_allowed(deps, path):
    runner = FakeNemohermes()
    with pytest.raises(InvalidInput):
        _resolver(deps, runner).resolve(f"sandbox:{path}")
    assert runner.calls == []                             # nothing was run


def test_failed_copy_is_a_retryable_error(deps):
    with pytest.raises(ExternalServiceError):
        _resolver(deps, FakeNemohermes(fail=True)).resolve("sandbox:/sandbox/.hermes/image_cache/x.jpg")


def test_local_files_are_refused_in_the_server(deps):
    resolver = ImageResolver(deps.db, deps.settings.upload_dir, allow_local_files=False)
    with pytest.raises(InvalidInput):
        resolver.resolve("file:/etc/hosts")
