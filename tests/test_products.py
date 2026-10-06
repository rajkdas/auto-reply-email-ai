"""Unit tests for the product catalog + fuzzy matcher."""
from __future__ import annotations

from pathlib import Path

from replydesk.models import Analysis, EmailMessage
from replydesk.products import ProductCatalog

PRODUCTS = Path(__file__).resolve().parents[1] / "products.example.yaml"


def _analysis(products: list[str]) -> Analysis:
    return Analysis(
        language="en",
        sentiment="negative",
        sentiment_score=-0.5,
        urgency="normal",
        intent="complaint",
        products_mentioned=products,
        summary="test",
        confidence=0.9,
    )


def _email(body: str) -> EmailMessage:
    return EmailMessage(
        message_id="<test@example.com>",
        subject="test",
        from_addr="a@example.com",
        body_plain=body,
    )


def test_catalog_load():
    cat = ProductCatalog.from_yaml(PRODUCTS, threshold=80)
    assert len(cat) == 4
    assert cat.get("widget_pro").name == "Widget Pro"


def test_exact_alias_match():
    cat = ProductCatalog.from_yaml(PRODUCTS, threshold=80)
    m = cat.match_raw("WidgetPro")
    assert m.catalog_id == "widget_pro"
    assert m.score == 100.0


def test_fuzzy_typo_match():
    cat = ProductCatalog.from_yaml(PRODUCTS, threshold=80)
    m = cat.match_raw("Widgt Pro")
    assert m.catalog_id == "widget_pro"
    assert m.score >= 80


def test_unknown_below_threshold():
    cat = ProductCatalog.from_yaml(PRODUCTS, threshold=95)
    # "WidgetProo" is a typo close to alias "WidgetPro", but not exact.
    m = cat.match_raw("WidgetProo")
    assert m.catalog_id is None


def test_sku_regex_pass():
    cat = ProductCatalog.from_yaml(PRODUCTS, threshold=80)
    em = _email("Order reference WP-1234 has not arrived.")
    a = _analysis([])
    matches = cat.detect(em, a)
    ids = [m.catalog_id for m in matches.matches if m.catalog_id]
    assert "widget_pro" in ids


def test_alias_substring_pass():
    cat = ProductCatalog.from_yaml(PRODUCTS, threshold=80)
    em = _email("Can you help me reset my CloudSync setup?")
    a = _analysis([])  # LLM didn't mention anything
    matches = cat.detect(em, a)
    ids = [m.catalog_id for m in matches.matches if m.catalog_id]
    assert "cloud_sync" in ids


def test_dedupe_raw_terms():
    cat = ProductCatalog.from_yaml(PRODUCTS, threshold=80)
    em = _email("WP-1234 is broken.")
    a = _analysis(["Widget Pro", "Widget Pro", "WidgetPro"])
    matches = cat.detect(em, a)
    # All three raw terms collapse to one widget_pro entry (deduped).
    assert len(matches.matches) <= 2  # widget_pro + maybe the SKU
    assert all(m.catalog_id == "widget_pro" for m in matches.matches
               if m.catalog_id)


def test_has_unknown_flag():
    cat = ProductCatalog.from_yaml(PRODUCTS, threshold=80)
    em = _email("Random gizmo X")
    a = _analysis(["Some Random Unknown Gizmo"])
    matches = cat.detect(em, a)
    assert matches.has_unknown is True


def test_faq_for_known_ids():
    cat = ProductCatalog.from_yaml(PRODUCTS, threshold=80)
    faq = cat.faq_for(["widget_pro", "cloud_sync"])
    assert "Widget Pro" in faq
    assert "Cloud Sync" in faq
    # Empty catalog_ids -> empty string
    assert cat.faq_for([]) == ""
