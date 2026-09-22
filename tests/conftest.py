import copy
import pytest
from automation.engine import Engine
from automation.workflow import Processor
from automation.fixtures import seed, model_answer


class FakeModel:
    def __init__(self):
        self.answer = model_answer()
        self.calls = 0

    def extract(self, *args):
        self.calls += 1
        return copy.deepcopy(self.answer), {"provider": "TEST_DOUBLE_NOT_AI", "model": "stub"}


@pytest.fixture
def fake_model():
    return FakeModel()


@pytest.fixture
def store(tmp_path, fake_model):
    engine = Engine(tmp_path / "test.sqlite3", lambda _: None, backoff=0)
    engine.processor = Processor(fake_model, engine)
    seed(engine)
    return engine


@pytest.fixture
def api(tmp_path, fake_model, monkeypatch):
    from automation.api import create_app
    for role in ("WEBHOOK", "OPERATOR", "APPROVAL"):
        monkeypatch.setenv(role + "_KEY", role + "-TEST-ONLY-KEY-123456789012345")
    app = create_app(tmp_path / "api.sqlite3", fake_model)
    seed(app.state.store)
    return app
