"""Focused smoke tests for get_nous_recommended_catalog + provider_model_ids('nous') rewiring."""
from unittest.mock import patch


def test_paid_catalog_preserves_server_order():
    from hermes_cli.models import get_nous_recommended_catalog
    payload = {
        "paidRecommendedModels": [
            {"modelName": "minimax/minimax-m2.7", "position": 0},
            {"modelName": "google/gemini-3-flash-preview", "position": 1},
            {"modelName": "openai/gpt-5.4", "position": 2},
        ],
        "freeRecommendedModels": [{"modelName": "free/one", "position": 0}],
    }
    with patch("hermes_cli.models.fetch_nous_recommended_models", return_value=payload):
        result = get_nous_recommended_catalog(free_tier=False)
    assert result == [
        "minimax/minimax-m2.7",
        "google/gemini-3-flash-preview",
        "openai/gpt-5.4",
    ]


def test_free_catalog_returns_free_list():
    from hermes_cli.models import get_nous_recommended_catalog
    payload = {
        "paidRecommendedModels": [{"modelName": "paid/one"}],
        "freeRecommendedModels": [
            {"modelName": "free/a"},
            {"modelName": "free/b"},
        ],
    }
    with patch("hermes_cli.models.fetch_nous_recommended_models", return_value=payload):
        result = get_nous_recommended_catalog(free_tier=True)
    assert result == ["free/a", "free/b"]


def test_empty_payload_returns_empty_list():
    from hermes_cli.models import get_nous_recommended_catalog
    with patch("hermes_cli.models.fetch_nous_recommended_models", return_value={}):
        assert get_nous_recommended_catalog(free_tier=False) == []
        assert get_nous_recommended_catalog(free_tier=True) == []


def test_missing_field_returns_empty_list():
    from hermes_cli.models import get_nous_recommended_catalog
    with patch(
        "hermes_cli.models.fetch_nous_recommended_models",
        return_value={"paidRecommendedModels": None},
    ):
        assert get_nous_recommended_catalog(free_tier=False) == []


def test_malformed_entries_skipped():
    from hermes_cli.models import get_nous_recommended_catalog
    payload = {
        "paidRecommendedModels": [
            {"modelName": "keep/a"},
            {},
            "not-a-dict",
            {"modelName": ""},
            {"modelName": "   "},
            {"modelName": "keep/b"},
        ]
    }
    with patch("hermes_cli.models.fetch_nous_recommended_models", return_value=payload):
        assert get_nous_recommended_catalog(free_tier=False) == ["keep/a", "keep/b"]


def test_dedup_case_insensitive_preserves_first_casing():
    from hermes_cli.models import get_nous_recommended_catalog
    payload = {
        "paidRecommendedModels": [
            {"modelName": "Foo"},
            {"modelName": "foo"},
            {"modelName": "FOO"},
            {"modelName": "Bar"},
        ]
    }
    with patch("hermes_cli.models.fetch_nous_recommended_models", return_value=payload):
        assert get_nous_recommended_catalog(free_tier=False) == ["Foo", "Bar"]


def test_auto_detect_free_tier_calls_check():
    from hermes_cli.models import get_nous_recommended_catalog
    payload = {
        "paidRecommendedModels": [{"modelName": "paid/x"}],
        "freeRecommendedModels": [{"modelName": "free/y"}],
    }
    with (
        patch("hermes_cli.models.fetch_nous_recommended_models", return_value=payload),
        patch("hermes_cli.models.check_nous_free_tier", return_value=True),
    ):
        assert get_nous_recommended_catalog() == ["free/y"]
    with (
        patch("hermes_cli.models.fetch_nous_recommended_models", return_value=payload),
        patch("hermes_cli.models.check_nous_free_tier", return_value=False),
    ):
        assert get_nous_recommended_catalog() == ["paid/x"]


def test_tier_detection_exception_defaults_to_paid():
    from hermes_cli.models import get_nous_recommended_catalog
    payload = {
        "paidRecommendedModels": [{"modelName": "paid/x"}],
        "freeRecommendedModels": [{"modelName": "free/y"}],
    }
    with (
        patch("hermes_cli.models.fetch_nous_recommended_models", return_value=payload),
        patch("hermes_cli.models.check_nous_free_tier", side_effect=RuntimeError("boom")),
    ):
        assert get_nous_recommended_catalog() == ["paid/x"]


def test_provider_model_ids_nous_uses_recommended_first():
    from hermes_cli import models as m
    with patch(
        "hermes_cli.models.get_nous_recommended_catalog",
        return_value=["portal/one", "portal/two"],
    ) as mock_rec:
        result = m.provider_model_ids("nous")
    assert result == ["portal/one", "portal/two"]
    mock_rec.assert_called_once()


def test_provider_model_ids_nous_falls_back_when_recommended_empty():
    """When Portal returns [] and inference API is unreachable, result is [].

    The hardcoded ``_PROVIDER_MODELS["nous"]`` catalog has been removed;
    Portal is now the sole source of truth (with the inference /models
    endpoint as a live fallback). No static fallback exists anymore.
    """
    from hermes_cli import models as m
    with (
        patch("hermes_cli.models.get_nous_recommended_catalog", return_value=[]),
        patch("hermes_cli.auth.resolve_nous_runtime_credentials", return_value=None),
    ):
        result = m.provider_model_ids("nous")
    assert result == []


def test_provider_model_ids_nous_falls_back_to_inference_models_endpoint():
    from hermes_cli import models as m
    with (
        patch("hermes_cli.models.get_nous_recommended_catalog", return_value=[]),
        patch(
            "hermes_cli.auth.resolve_nous_runtime_credentials",
            return_value={"api_key": "k", "base_url": "https://x"},
        ),
        patch(
            "hermes_cli.auth.fetch_nous_models",
            return_value=["infer/a", "infer/b"],
        ),
    ):
        result = m.provider_model_ids("nous")
    assert result == ["infer/a", "infer/b"]


def test_provider_model_ids_nous_recommended_exception_falls_through():
    from hermes_cli import models as m
    with (
        patch(
            "hermes_cli.models.get_nous_recommended_catalog",
            side_effect=RuntimeError("portal down"),
        ),
        patch(
            "hermes_cli.auth.resolve_nous_runtime_credentials",
            return_value={"api_key": "k", "base_url": "https://x"},
        ),
        patch(
            "hermes_cli.auth.fetch_nous_models",
            return_value=["infer/a"],
        ),
    ):
        result = m.provider_model_ids("nous")
    assert result == ["infer/a"]


def test_provider_model_ids_force_refresh_propagates():
    from hermes_cli import models as m
    with patch(
        "hermes_cli.models.get_nous_recommended_catalog",
        return_value=["x/y"],
    ) as mock_rec:
        m.provider_model_ids("nous", force_refresh=True)
    assert mock_rec.call_args.kwargs.get("force_refresh") is True


# ---------------------------------------------------------------------------
# Hardcoded-list removal: everything that used to read _PROVIDER_MODELS["nous"]
# now routes through _nous_catalog() → Portal.
# ---------------------------------------------------------------------------

def test_nous_key_not_in_static_provider_models():
    """_PROVIDER_MODELS must not contain a hardcoded nous entry."""
    from hermes_cli.models import _PROVIDER_MODELS
    assert "nous" not in _PROVIDER_MODELS


def test_get_default_model_for_provider_nous_uses_portal():
    from hermes_cli import models as m
    with patch(
        "hermes_cli.models._nous_catalog",
        return_value=["portal/first", "portal/second"],
    ) as mock_cat:
        assert m.get_default_model_for_provider("nous") == "portal/first"
    mock_cat.assert_called_once()


def test_get_default_model_for_provider_nous_empty_returns_empty_string():
    from hermes_cli import models as m
    with patch("hermes_cli.models._nous_catalog", return_value=[]):
        assert m.get_default_model_for_provider("nous") == ""


def test_get_default_model_for_provider_other_providers_unaffected():
    """Non-nous providers must still read from _PROVIDER_MODELS."""
    from hermes_cli import models as m
    # gemini has a static catalog; first entry should be returned without
    # touching the Portal helper.
    with patch("hermes_cli.models._nous_catalog") as mock_cat:
        result = m.get_default_model_for_provider("gemini")
    assert result  # non-empty
    mock_cat.assert_not_called()


def test_detect_bare_provider_name_nous_uses_portal():
    """`/model nous` typed as a model name → switch to nous + Portal's first model."""
    from hermes_cli import models as m
    with patch(
        "hermes_cli.models._nous_catalog",
        return_value=["portal/first", "portal/second"],
    ):
        result = m.detect_provider_for_model("nous", current_provider="anthropic")
    assert result == ("nous", "portal/first")


def test_detect_bare_provider_name_nous_empty_no_switch():
    """When Portal returns [], bare `/model nous` should NOT claim a switch."""
    from hermes_cli import models as m
    with patch("hermes_cli.models._nous_catalog", return_value=[]):
        # detect_provider_for_model should not return a spurious (nous, "")
        result = m.detect_provider_for_model("nous", current_provider="anthropic")
    # Either None or a non-nous match is acceptable; the forbidden outcome
    # is (nous, "") or (nous, "nous").
    if result is not None:
        pid, model = result
        assert not (pid == "nous" and model in ("", "nous"))


def test_detect_current_nous_provider_uses_portal_catalog():
    """Model already in nous's Portal catalog → no auto-switch when current=nous."""
    from hermes_cli import models as m
    with patch(
        "hermes_cli.models._nous_catalog",
        return_value=["anthropic/claude-sonnet-4.6", "openai/gpt-5.4"],
    ):
        # User is on nous, types a model that IS in nous's Portal catalog.
        # detect_provider_for_model should return None (no switch needed).
        result = m.detect_provider_for_model(
            "anthropic/claude-sonnet-4.6",
            current_provider="nous",
        )
    assert result is None


def test_nous_catalog_wraps_exception_to_empty_list():
    """_nous_catalog swallows exceptions from get_nous_recommended_catalog."""
    from hermes_cli import models as m
    with patch(
        "hermes_cli.models.get_nous_recommended_catalog",
        side_effect=RuntimeError("portal down"),
    ):
        assert m._nous_catalog() == []


def test_curated_models_for_provider_nous_routes_through_portal():
    """curated_models_for_provider('nous') → Portal list via provider_model_ids."""
    from hermes_cli import models as m
    with patch(
        "hermes_cli.models.get_nous_recommended_catalog",
        return_value=["portal/a", "portal/b"],
    ):
        tuples = m.curated_models_for_provider("nous")
    ids = [mid for mid, _ in tuples]
    assert ids == ["portal/a", "portal/b"]
