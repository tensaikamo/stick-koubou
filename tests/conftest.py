"""テスト共通設定。リポジトリ直下を import パスに追加し、各テストを一時 cwd で走らせる。
すべてオフライン(ネットワーク・実API不要)。fake seam(RECORDER_FAKE_RESPONSE /
RESOLVER_FAKE_RESPONSE)と fetch のモンキーパッチで外部依存を断つ。"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    return tmp_path
