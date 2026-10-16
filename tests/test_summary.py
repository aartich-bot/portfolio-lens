"""Optional summaries never invent figures or send transaction records."""

import json
import os
from types import SimpleNamespace

import pytest

from portfolio_lens.services import summary


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(summary, "PROJECT_ROOT", tmp_path / "project")
    (tmp_path / "project").mkdir()


def facts():
    return (
        summary.AggregateFact(
            "value",
            "Current value",
            "$1,000.00",
            "Current portfolio value is $1,000.00.",
            ("Value uses the latest available prices.",),
        ),
        summary.AggregateFact(
            "risk",
            "Largest holding",
            "40%",
            "The largest holding represents 40% of value.",
            ("Concentration makes results sensitive to one holding.",),
        ),
    )


class FakeAPI:
    def __init__(self, payload=None, *, exception=None, status="completed", output_text=None):
        self.payload = payload
        self.exception = exception
        self.status = status
        self.output_text = output_text
        self.calls = []
        self.options = []

    def factory(self, **kwargs):
        self.options.append(kwargs)
        return SimpleNamespace(responses=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exception:
            raise self.exception
        return SimpleNamespace(
            status=self.status,
            output_text=(
                json.dumps(self.payload) if self.output_text is None else self.output_text
            ),
        )


def selection(*pairs):
    return {
        "selections": [
            {"fact_id": fact, "explanation_id": explanation} for fact, explanation in pairs
        ]
    }


def test_missing_key_renders_verified_facts_and_explanations_without_api():
    api = FakeAPI()
    result = summary.generate_summary(
        facts(), environment={}, client_factory=api.factory, fingerprint="current-state"
    )
    assert result.source == "rule_based"
    assert "Current portfolio value is $1,000.00." in result.text
    assert "Concentration makes results sensitive to one holding." in result.text
    assert "key" in result.notice.lower()
    assert result.fingerprint == "current-state"
    assert api.calls == []


def test_rule_based_summary_never_reads_configuration(monkeypatch):
    def forbidden_access(*args, **kwargs):
        raise AssertionError("Default summary must not read configuration or contact an API.")

    monkeypatch.setattr(summary, "dotenv_values", forbidden_access)
    result = summary.rule_based_summary(facts(), fingerprint="default-state")
    assert result.source == "rule_based"
    assert "Current portfolio value is $1,000.00." in result.text
    assert "Concentration makes results sensitive to one holding." in result.text
    assert result.fingerprint == "default-state"


def test_rule_based_summary_preserves_explicit_notice():
    result = summary.rule_based_summary(facts(), notice="Current aggregates only.")
    assert result.notice == "Current aggregates only."


def test_ai_selects_order_but_locally_verified_text_is_rendered():
    api = FakeAPI(selection(("risk", "risk:explanation:0"), ("value", None)))
    result = summary.generate_summary(
        facts(), environment={"OPENAI_API_KEY": "fake-key"}, client_factory=api.factory
    )
    assert result.source == "ai"
    assert result.text.startswith("The largest holding represents 40% of value.")
    assert "Concentration makes results sensitive to one holding." in result.text
    assert result.text.endswith("Current portfolio value is $1,000.00.")
    assert "selection" in result.notice.lower()


@pytest.mark.parametrize(
    "payload",
    [
        selection(("made-up", None)),
        selection(("risk", "value:explanation:0")),
        {"selections": [{"fact_id": "risk", "explanation_id": None, "text": "Profit is 999%."}]},
        {"selections": [{"fact_id": "risk", "explanation_id": None}], "extra": "999%"},
        selection(("risk", None), ("risk", None)),
        {"selections": []},
        {"selections": "untrusted text"},
    ],
)
def test_invalid_ai_output_falls_back_without_rendering_model_text(payload):
    api = FakeAPI(payload)
    result = summary.generate_summary(
        facts(), environment={"OPENAI_API_KEY": "fake-key"}, client_factory=api.factory
    )
    assert result.source == "rule_based"
    assert "Current portfolio value is $1,000.00." in result.text
    assert "999" not in result.text + result.notice


def test_api_failure_notice_does_not_expose_response_body_or_key():
    api = FakeAPI(exception=RuntimeError("sk-secret BrokerSecret transaction 2099-10-01"))
    result = summary.generate_summary(
        facts(), environment={"OPENAI_API_KEY": "fake-key"}, client_factory=api.factory
    )
    assert result.source == "rule_based"
    assert "unavailable" in result.notice.lower()
    assert "sk-secret" not in result.notice + result.text
    assert "BrokerSecret" not in result.notice + result.text


def test_request_contains_only_catalog_with_privacy_and_resource_limits():
    api = FakeAPI(selection(("value", None)))
    result = summary.generate_summary(
        facts(), environment={"OPENAI_API_KEY": "fake-key"}, client_factory=api.factory
    )
    assert result.source == "ai"
    assert api.options == [{"api_key": "fake-key", "timeout": 20.0, "max_retries": 0}]
    call = api.calls[0]
    assert call["store"] is False
    assert call["model"] == "gpt-4.1-mini"
    assert 0 < call["max_output_tokens"] <= 1200
    catalog = json.loads(call["input"][1]["content"])
    assert list(catalog) == ["facts"]
    assert list(catalog["facts"][0]) == ["id", "label", "value", "statement", "explanations"]
    assert set(catalog["facts"][0]["explanations"][0]) == {"id", "text"}
    serialized = json.dumps(call)
    for forbidden in ("broker", "source_row", "split_convention", "transaction_type", "quantity"):
        assert forbidden not in serialized.lower()
    format_config = call["text"]["format"]
    assert format_config["type"] == "json_schema"
    assert format_config["strict"] is True


def test_no_facts_means_no_api_request():
    api = FakeAPI()
    result = summary.generate_summary(
        (), environment={"OPENAI_API_KEY": "fake-key"}, client_factory=api.factory
    )
    assert result.source == "rule_based"
    assert "unavailable" in result.text.lower()
    assert not api.calls


def test_transaction_dictionaries_are_rejected_without_any_api_request():
    api = FakeAPI()
    with pytest.raises(TypeError, match="AggregateFact"):
        summary.generate_summary(
            [{"broker": "Secret", "quantity": 999}],
            environment={"OPENAI_API_KEY": "fake-key"},
            client_factory=api.factory,
        )
    assert not api.calls


def test_project_dotenv_only_and_environment_precedence_without_mutation(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=parent-secret\n", encoding="utf-8")
    (summary.PROJECT_ROOT / ".env").write_text(
        "OPENAI_API_KEY=local-fake-key\nOPENAI_MODEL=local-model\n", encoding="utf-8"
    )
    before = dict(os.environ)
    api = FakeAPI(selection(("value", None)))
    result = summary.generate_summary(
        facts(), environment={"OPENAI_MODEL": "env-model"}, client_factory=api.factory
    )
    assert result.source == "ai"
    assert api.options[0]["api_key"] == "local-fake-key"
    assert api.calls[0]["model"] == "env-model"
    assert dict(os.environ) == before


def test_adjacent_dotenv_key_is_never_used(tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=parent-secret\n", encoding="utf-8")
    api = FakeAPI()
    result = summary.generate_summary(facts(), environment={}, client_factory=api.factory)
    assert result.source == "rule_based"
    assert not api.calls


def test_blank_environment_key_disables_local_dotenv_key():
    (summary.PROJECT_ROOT / ".env").write_text("OPENAI_API_KEY=local-fake-key\n", encoding="utf-8")
    api = FakeAPI()
    result = summary.generate_summary(
        facts(), environment={"OPENAI_API_KEY": ""}, client_factory=api.factory
    )
    assert result.source == "rule_based"
    assert not api.calls


@pytest.mark.parametrize("status", ["incomplete", "failed", None])
def test_incomplete_response_falls_back(status):
    api = FakeAPI(selection(("value", None)), status=status)
    result = summary.generate_summary(
        facts(), environment={"OPENAI_API_KEY": "fake-key"}, client_factory=api.factory
    )
    assert result.source == "rule_based"


def test_oversized_catalog_uses_rule_fallback_without_remote_request():
    oversized = [
        summary.AggregateFact(f"fact-{i}", "Value", "1", "Verified value is 1.") for i in range(100)
    ]
    api = FakeAPI()
    result = summary.generate_summary(
        oversized, environment={"OPENAI_API_KEY": "fake-key"}, client_factory=api.factory
    )
    assert result.source == "rule_based"
    assert len(result.text) < 2000
    assert not api.calls


@pytest.mark.parametrize(
    "output_text",
    [
        "not JSON",
        '{"selections":[],"selections":[{"fact_id":"value","explanation_id":null}]}',
        '{"selections":[{"fact_id":"risk","fact_id":"value","explanation_id":null}]}',
        "x" * 20000,
    ],
)
def test_invalid_or_oversized_json_uses_verified_fallback(output_text):
    api = FakeAPI(output_text=output_text)
    result = summary.generate_summary(
        facts(), environment={"OPENAI_API_KEY": "fake-key"}, client_factory=api.factory
    )
    assert result.source == "rule_based"
    assert result.text.startswith("Current portfolio value is $1,000.00.")


def test_large_utf8_catalog_never_sent_even_when_each_field_is_in_bounds():
    catalog = [
        summary.AggregateFact(
            f"fact-{i}", "Aggregate", "1", "Verified aggregate.", ("é" * 600,) * 4
        )
        for i in range(8)
    ]
    api = FakeAPI()
    result = summary.generate_summary(
        catalog, environment={"OPENAI_API_KEY": "fake-key"}, client_factory=api.factory
    )
    assert result.source == "rule_based"
    assert not api.calls


def test_catalog_without_explanations_supports_ai_selection():
    api = FakeAPI(selection(("value", None)))
    catalog = (summary.AggregateFact("value", "Value", "$100", "Current value is $100."),)
    result = summary.generate_summary(
        catalog, environment={"OPENAI_API_KEY": "fake-key"}, client_factory=api.factory
    )
    assert result.source == "ai"
    assert result.text == "Current value is $100."


def test_request_never_contains_local_fingerprint():
    api = FakeAPI(selection(("value", None)))
    result = summary.generate_summary(
        facts(),
        fingerprint="private-import-state",
        environment={"OPENAI_API_KEY": "fake-key"},
        client_factory=api.factory,
    )
    assert result.fingerprint == "private-import-state"
    assert "private-import-state" not in json.dumps(api.calls)


def test_rule_summary_rejects_raw_rows():
    with pytest.raises(TypeError, match="AggregateFact"):
        summary.rule_based_summary([{"broker": "Private broker"}])


def test_bad_fact_text_rejected_before_configuration_or_api(monkeypatch):
    def forbidden_access(*args, **kwargs):
        raise AssertionError("Malformed input must be rejected before settings are read.")

    monkeypatch.setattr(summary, "dotenv_values", forbidden_access)
    with pytest.raises(ValueError, match="approved text"):
        summary.generate_summary(
            [summary.AggregateFact("value", "Value", 42, "A fact.")],
            environment={"OPENAI_API_KEY": "fake-key"},
        )
