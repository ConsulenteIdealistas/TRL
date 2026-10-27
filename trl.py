"""TRL投資分析システム 互換入口。実装は trlcore/ にある。
`python trl.py ...` と `import trl as T` の両方を従来どおり使えるように、全ての名前を再公開する。"""
import argparse  # noqa: F401
import calendar  # noqa: F401
import hashlib  # noqa: F401
import json  # noqa: F401
import math  # noqa: F401
import os  # noqa: F401
import random  # noqa: F401
import re  # noqa: F401
import sqlite3  # noqa: F401
import sys  # noqa: F401
import time  # noqa: F401
import unicodedata  # noqa: F401
import urllib.error  # noqa: F401
import urllib.parse  # noqa: F401
import urllib.request  # noqa: F401
import uuid  # noqa: F401
import xml.etree.ElementTree as ET  # noqa: F401
from datetime import date, datetime, timedelta, timezone  # noqa: F401
from pathlib import Path  # noqa: F401
from trlcore.errors import ConfigError, HttpError, LLMError, PromptError  # noqa: F401,E402
from trlcore.dates import _RE, parse_date, days_since, freshness_info  # noqa: F401,E402
from trlcore.http import SECRET_ENV, _PARAM_RE, redact, _retry_after_seconds, backoff_delay, http_fetch  # noqa: F401,E402
from trlcore.normalize import clean_text, norm_doi, norm_url, FATAL, normalize  # noqa: F401,E402
from trlcore.sources.pubmed import BASE, MONTHS, _month, _date_from, parse_efetch, pubmed_fetch  # noqa: F401,E402
from trlcore.sources.arxiv import URL, NS, _last_call, parse_feed, arxiv_fetch  # noqa: F401,E402
from trlcore.sources.gnews import parse_feed as gnews_parse_feed, gnews_fetch, prtimes_fetch  # noqa: F401,E402
from trlcore.sources.trials import parse_studies, trials_fetch  # noqa: F401,E402
from trlcore.sources.edinet import sec_code, parse_results as edinet_parse_results, edinet_fetch  # noqa: F401,E402
from trlcore.sources.edinet import parse_facts_csv as edinet_parse_facts_csv, parse_facts_xbrl as edinet_parse_facts_xbrl  # noqa: F401,E402
from trlcore.sources.edinet import select_financials as edinet_select_financials, format_financials as edinet_format_financials  # noqa: F401,E402
from trlcore.sources.edinet import fetch_financial_text as edinet_fetch_financial_text, FINANCIAL_MARKER as EDINET_FINANCIAL_MARKER  # noqa: F401,E402
from trlcore.sources.yahoo import yahoo_fetch, parse_quote_summary as yahoo_parse_quote_summary  # noqa: F401,E402
from trlcore.sources.openalex import openalex_fetch, parse_works as openalex_parse_works  # noqa: F401,E402
from trlcore.translate import needs_translation, translate_batch, translate_display_set  # noqa: F401,E402
from trlcore.scoring import item_keys, validate_items, band_for, compute  # noqa: F401,E402
from trlcore.entities import _norm_text, ENTITY_MODES, validate_entities, _contains, link_entities, entity_screen  # noqa: F401,E402
from trlcore.config import __version__, ROOT, CONFIG_FILES, LLM_DEFAULTS, SOURCE_NAMES, validate_config, validate_sources, build_entity_queries, required_files, missing_files, load_config  # noqa: F401,E402
from trlcore.store import SCHEMA, now_iso, _j, Store  # noqa: F401,E402
from trlcore.prompts import PROMPT_DIR, _PH, load_prompt, render  # noqa: F401,E402
from trlcore.llm import SYSTEM, extract_json, Anthropic, Gemini, Mock, get_provider, llm_settings, configure_llm, translation_llm, _fallback_model  # noqa: F401,E402
from trlcore.evaluate import EVIDENCE_KINDS, EXTRACT_LISTS, STATEMENT_LISTS, validate_extraction, _nonempty_list, validate_assessment, parse_trl_level, parse_direction, DIRECTIONS, _candidate_block, input_hash, run_extraction, evaluate_one  # noqa: F401,E402
from trlcore.pipeline import DEFAULT_FETCHERS, new_run_id, screen, collect, select_for_eval, has_demo_signal, _queue_reason, run_pipeline  # noqa: F401,E402
from trlcore.site import SITE_SCHEMA_VERSION, TECH_ITEMS, LEGACY_INFRA_QUEUE_PREFIX, _ev_for_site, display_status, build_site_data, export_site  # noqa: F401,E402
from trlcore.report import build_research_links, format_research_links, DISCLAIMER, build_report  # noqa: F401,E402
from trlcore.cli import _prune, main  # noqa: F401,E402


if __name__ == "__main__":
    sys.exit(main())
