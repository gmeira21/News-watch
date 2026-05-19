Not used in current architecture; reserved for future LLM-based
enrichment if pure HTML discovery proves insufficient.

Note: PDF discovery is best-effort. A sourced_post item without a
pdf_url is fine — the article URL itself is the citable source. The
verdict only downgrades to news_repost on actual enrichment errors,
not on "no PDF on page."
