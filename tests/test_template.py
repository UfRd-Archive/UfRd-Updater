from __future__ import annotations

import pytest

from ufrd_updater.template import (
    TemplateError,
    render,
    render_json,
    template_fields,
)


def test_literal_braces_and_fields():
    assert render("{{literal}} {name} }}", {"name": "value"}) == "{literal} value }"
    assert render("no fields", {}) == "no fields"
    assert template_fields("{a}{b}") == ["a", "b"]


def test_environment_variables():
    assert render("{env:UFRD_TEST}", {}, environ={"UFRD_TEST": "from-env"}) == "from-env"
    with pytest.raises(TemplateError, match="environment variable 'UFRD_TEST' is not set"):
        render("{env:UFRD_TEST}", {}, environ={})
    with pytest.raises(TemplateError, match="empty environment"):
        render("{env:}", {}, environ={})


def test_missing_variable_lists_names_not_values():
    with pytest.raises(TemplateError) as error:
        render("{missing}", {"secret": "hunter2", "other": "x"})
    message = str(error.value)
    assert "missing" in message
    assert "secret" in message
    assert "other" in message
    assert "hunter2" not in message


def test_non_scalar_values_are_rejected():
    with pytest.raises(TemplateError, match="use an extractor index"):
        render("{items}", {"items": ["a"]})
    with pytest.raises(TemplateError, match="dict, not a scalar"):
        render("{obj}", {"obj": {"a": 1}})


@pytest.mark.parametrize(
    "template",
    [
        "{name!r}",
        "{name:>10}",
        "{obj.attr}",
        "{items[0]}",
        "{}",
        "{unclosed",
    ],
)
def test_unsupported_template_syntax(template):
    with pytest.raises(TemplateError):
        render(template, {"name": "x", "obj": {}})


def test_render_json_recurses_and_uses_environment():
    value = {
        "channel": "{channel}",
        "nested": ["{env:UFRD_TEST}", 7, True, None],
    }
    rendered = render_json(
        value,
        {"channel": "stable"},
        environ={"UFRD_TEST": "env-value"},
    )
    assert rendered == {
        "channel": "stable",
        "nested": ["env-value", 7, True, None],
    }
