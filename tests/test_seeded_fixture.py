from pathlib import Path

from argus.context.git import local_context
from tests.helpers import SEEDED_FILES, build_seeded_repo


def test_seeded_repo_diff_touches_exactly_the_seeded_files(tmp_path: Path) -> None:
    repo = build_seeded_repo(tmp_path / "seeded")

    ctx = local_context(repo, base="main")

    assert sorted(f.path for f in ctx.files) == sorted(SEEDED_FILES)
    assert '+    cursor = conn.execute(f"SELECT' in ctx.diff_text
    assert "start + size - 1" in ctx.diff_text
