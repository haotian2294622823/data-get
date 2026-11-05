import pytest
from streamlit.testing.v1 import AppTest
from src.catalog import BASE


@pytest.fixture(autouse=True)
def environment(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "test-only-shared-password")
    monkeypatch.setenv("DATA_SOURCE_MODE", "mock")


def app():
    at = AppTest.from_file(str(BASE / "app.py"), default_timeout=20).run()
    assert not at.exception
    return at


def login(at):
    at.text_input(key="password").set_value("test-only-shared-password")
    at.button(key="login_submit").click().run()
    assert not at.exception


def traced():
    at = app()
    login(at)
    at.text_input(key="root_lot").set_value("ROOT_A")
    at.button(key="trace_submit").click().run()
    assert not at.exception
    return at


def test_locked_wrong_password_then_logout():
    at = app()
    assert not at.get("download_button")
    assert not at.text_input(key="password").value
    at.text_input(key="password").set_value("wrong")
    at.button(key="login_submit").click().run()
    assert not at.session_state["authenticated"]
    login(at)
    at.button(key="logout").click().run()
    assert not at.session_state["authenticated"]
    assert at.session_state["query_result"] is None


def test_two_submissions_and_new_root_clears_download():
    at = traced()
    assert len(at.session_state["trace_result"].paths) == 8
    assert not at.get("download_button")
    at.button(key="extract_submit").click().run()
    assert not at.exception
    assert len(at.session_state["query_result"].datasets) == 4
    at.text_input(key="root_lot").set_value("NOT_FOUND")
    at.button(key="trace_submit").click().run()
    assert at.session_state["query_result"] is None
    assert at.session_state["trace_state"] == "empty"
    assert not at.get("download_button")


def test_failed_extraction_removes_previous_csv():
    at = traced()
    at.button(key="extract_submit").click().run()
    at.multiselect(key="datasets").set_value([])
    at.button(key="extract_submit").click().run()
    assert not at.exception
    assert at.session_state["query_result"] is None
    assert not at.get("download_button")


def test_drafts_and_preview_do_not_read_source(monkeypatch):
    from src.data_access.mock import MockFactory

    original = MockFactory.open_read_session
    calls = []

    def counted(self, deadline_at):
        calls.append(deadline_at)
        return original(self, deadline_at)

    monkeypatch.setattr(MockFactory, "open_read_session", counted)
    at = traced()
    assert len(calls) == 1
    at.button(key="extract_submit").click().run()
    assert len(calls) == 2
    saved = at.session_state["query_result"]
    at.selectbox(key="view_dataset").set_value("material_records").run()
    assert len(calls) == 2
    assert at.session_state["query_result"].trace.trace_id == saved.trace.trace_id
    fresh = app()
    assert not fresh.session_state["authenticated"]
    assert fresh.session_state["query_result"] is None


def test_password_missing_blocks_everything(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    at = app()
    assert at.error and not at.get("download_button")
    assert at.session_state["trace_result"] is None


def test_login_unlocks_without_browser_refresh(monkeypatch):
    from types import SimpleNamespace
    import src.ui.login as auth

    now = [100.0]
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: now[0]))
    at = app()
    for _ in range(5):
        at.text_input(key="password").set_value("wrong")
        at.button(key="login_submit").click().run()
    # 仍允许提交以刷新锁定判断，后端在期限内拒绝认证。
    assert not at.button(key="login_submit").disabled
    now[0] = 131.0
    login(at)
    assert at.session_state["authenticated"]


def test_diagnostic_tables_have_no_implicit_csv_toolbar():
    at = traced()
    # dataframe 自带“导出预览CSV”工具栏，会绕过正式生成动作和超限禁下载约定。
    assert not at.dataframe
