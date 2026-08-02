"""The Streamlit app renders.

These run the real app script headlessly against whatever is in the local
warehouse, so they skip when nothing has been ingested. They are the only tests
that exercise the page code itself; the query layer is covered without Streamlit
in `tests/test_read_path.py`.
"""

from __future__ import annotations

import os
import resource
import time
from pathlib import Path

import pytest

from finlab.config import Settings

APP = Path(__file__).resolve().parent.parent / "src" / "finlab" / "ui" / "app.py"

pytest.importorskip("streamlit", reason="the UI extra is not installed")
from streamlit.testing.v1 import AppTest  # noqa: E402


@pytest.fixture(scope="module")
def local_warehouse() -> Settings:
    """The developer's own data tree, skipped when it has not been ingested."""
    for key in [k for k in os.environ if k.startswith("FINLAB_")]:
        os.environ.pop(key)
    settings = Settings()
    if not (settings.warehouse_dir / "fact").is_dir():
        pytest.skip("no warehouse ingested — run `finlab ingest poc` first")
    return settings


@pytest.fixture
def app(local_warehouse: Settings) -> AppTest:
    return AppTest.from_file(str(APP), default_timeout=120)


def test_the_app_boots_within_three_seconds(app: AppTest) -> None:
    started = time.monotonic()
    app.run()
    elapsed = time.monotonic() - started
    assert not app.exception, app.exception
    assert elapsed < 3.0, f"cold render took {elapsed:.2f}s"


def test_every_page_renders(app: AppTest) -> None:
    app.run()
    assert not app.exception, app.exception
    for page in ("Screen", "Company", "Restatement explorer"):
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, f"{page}: {app.exception}"


def test_moving_the_as_of_slider_re_renders(app: AppTest) -> None:
    """The slider must drive a real re-resolution, not just a label."""
    app.run()
    slider = app.sidebar.slider[0]
    started = time.monotonic()
    slider.set_value(slider.value.replace(year=slider.value.year - 3)).run()
    elapsed = time.monotonic() - started
    assert not app.exception, app.exception
    assert elapsed < 1.0, f"re-render took {elapsed:.2f}s"


def test_pyarrow_is_pinned_below_the_version_that_segfaults() -> None:
    """pyarrow 25.0.0 segfaults converting a DataFrame to Arrow on Streamlit's
    script thread, killing the app on the first slider move. Reproducible
    without any finlab code, so the cap in pyproject.toml is load-bearing."""
    import pyarrow

    major = int(pyarrow.__version__.split(".")[0])
    assert major < 25, f"pyarrow {pyarrow.__version__} reintroduces the UI segfault"


def test_the_app_stays_within_its_memory_budget(app: AppTest) -> None:
    """A screen over the whole universe must not materialise the fact table."""
    app.run()
    app.sidebar.radio[0].set_value("Screen").run()
    assert not app.exception, app.exception
    peak_mib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    assert peak_mib < 1024, f"peak RSS {peak_mib:.0f} MiB exceeds the 1 GB budget"
