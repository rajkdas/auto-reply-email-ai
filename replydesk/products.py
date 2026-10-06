"""Product catalog loader + fuzzy matcher.

Detection pipeline (per email):
1. Take ``Analysis.products_mentioned`` (raw strings the LLM saw).
2. Add anything matched by ``sku_pattern`` regex on the email body.
3. Add anything matched by exact alias name on the email body.
4. For each raw string, find the best catalog match using rapidfuzz
   (token_sort_ratio) against the product name + aliases.
5. If the best score is below ``FUZZY_MATCH_THRESHOLD``, mark it ``unknown``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from .models import Analysis, EmailMessage, ProductMatch, ProductMatches

log = logging.getLogger(__name__)


@dataclass
class Product:
    id: str
    name: str
    aliases: list[str]
    sku_pattern: re.Pattern | None = None
    faq: str = ""

    @property
    def match_terms(self) -> list[str]:
        """All strings worth fuzzy-matching against."""
        terms = [self.name] + list(self.aliases)
        return [t for t in terms if t]


class ProductCatalog:
    """In-memory catalog with a fuzzy matcher."""

    def __init__(self, products: list[Product], threshold: float = 80.0):
        self._products = products
        self._by_id: dict[str, Product] = {p.id: p for p in products}
        self.threshold = threshold

    # -- construction ----------------------------------------------------
    @classmethod
    def from_yaml(cls, path: str | Path, threshold: float = 80.0) -> ProductCatalog:
        path = Path(path)
        if not path.exists():
            log.warning("Products file %s not found; catalog is empty.", path)
            return cls([], threshold=threshold)
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        items = data.get("products", []) or []
        products: list[Product] = []
        for it in items:
            sku_pat = it.get("sku_pattern")
            products.append(
                Product(
                    id=it["id"],
                    name=it["name"],
                    aliases=list(it.get("aliases", []) or []),
                    sku_pattern=re.compile(sku_pat) if sku_pat else None,
                    faq=it.get("faq", "") or "",
                )
            )
        return cls(products, threshold=threshold)

    # -- queries ---------------------------------------------------------
    def __len__(self) -> int:
        return len(self._products)

    def __iter__(self) -> Iterable[Product]:
        return iter(self._products)

    def get(self, product_id: str) -> Product | None:
        return self._by_id.get(product_id)

    def faq_for(self, catalog_ids: Iterable[str]) -> str:
        """Concatenate FAQ snippets for the given catalog IDs."""
        chunks: list[str] = []
        for pid in catalog_ids:
            p = self._by_id.get(pid)
            if p and p.faq:
                chunks.append(f"[{p.name}]\n{p.faq.strip()}")
        return "\n\n".join(chunks)

    # -- matching --------------------------------------------------------
    def match_raw(self, raw: str) -> ProductMatch:
        """Find the best catalog product for a raw string."""
        raw_norm = (raw or "").strip()
        if not raw_norm:
            return ProductMatch(raw=raw, catalog_id=None, score=0.0)

        # Exact alias/name match short-circuits.
        for p in self._products:
            for term in p.match_terms:
                if term and term.lower() == raw_norm.lower():
                    return ProductMatch(
                        raw=raw,
                        catalog_id=p.id,
                        catalog_name=p.name,
                        score=100.0,
                    )

        # Fuzzy: best score across name + aliases.
        best_id: str | None = None
        best_name: str | None = None
        best_score = 0.0
        for p in self._products:
            for term in p.match_terms:
                score = fuzz.token_sort_ratio(raw_norm.lower(), term.lower())
                if score > best_score:
                    best_score = score
                    best_id = p.id
                    best_name = p.name

        if best_score >= self.threshold and best_id:
            return ProductMatch(
                raw=raw,
                catalog_id=best_id,
                catalog_name=best_name,
                score=best_score,
            )
        return ProductMatch(raw=raw, catalog_id=None, score=best_score)

    def detect(self, email: EmailMessage, analysis: Analysis) -> ProductMatches:
        """Run the full detection pipeline on one email."""
        # Each raw term we collect is paired with an optional forced catalog_id
        # (used for SKU matches, where we already know which product owns the
        # pattern and don't need fuzzy matching).
        raw_terms: list[tuple[str, str | None]] = [
            (rt, None) for rt in (analysis.products_mentioned or [])
        ]

        # SKU regex pass on the email body. Each match is force-mapped to the
        # product whose sku_pattern matched it.
        body = email.body_plain or ""
        for p in self._products:
            if p.sku_pattern:
                for m in p.sku_pattern.findall(body):
                    s = m if isinstance(m, str) else (m[0] if m else "")
                    if s:
                        raw_terms.append((s, p.id))

        # Alias substring pass (case-insensitive). Force-map to the product
        # whose alias matched.
        body_lc = body.lower()
        for p in self._products:
            for alias in p.aliases:
                if alias and alias.lower() in body_lc:
                    raw_terms.append((alias, p.id))

        # De-duplicate raw terms (case-insensitive). When the same raw text
        # appears multiple times, keep the first forced catalog_id we saw.
        seen: dict[str, str | None] = {}
        order: list[str] = []
        for rt, forced in raw_terms:
            key = (rt or "").strip().lower()
            if not key:
                continue
            if key not in seen:
                seen[key] = forced
                order.append(key)

        matches: list[ProductMatch] = []
        for key in order:
            # Find the original raw spelling (first occurrence with this key).
            raw_spelling = next(
                (rt for rt, _ in raw_terms if (rt or "").strip().lower() == key),
                key,
            )
            forced = seen[key]
            if forced:
                p = self._by_id[forced]
                matches.append(ProductMatch(
                    raw=raw_spelling, catalog_id=p.id, catalog_name=p.name,
                    score=100.0,
                ))
            else:
                matches.append(self.match_raw(raw_spelling))

        # Collapse: keep one entry per catalog_id when multiple raw spellings
        # map to the same product. Always keep unknown entries (catalog_id is
        # None) so they're visible to the rules engine.
        seen_ids: dict[str, ProductMatch] = {}
        unknowns: list[ProductMatch] = []
        for m in matches:
            if m.catalog_id is None:
                unknowns.append(m)
            else:
                if m.catalog_id not in seen_ids:
                    seen_ids[m.catalog_id] = m
        return ProductMatches(matches=list(seen_ids.values()) + unknowns)


__all__ = ["Product", "ProductCatalog"]
