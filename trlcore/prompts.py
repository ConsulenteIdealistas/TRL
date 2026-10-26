"""プロンプトの読み込みと差し込み。バージョンと内容ハッシュを評価結果に紐づける。"""

import hashlib
import re
from pathlib import Path
from .errors import PromptError
from .config import ROOT


PROMPT_DIR = ROOT


_PH = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")


def load_prompt(name, version, prompt_dir=None):
    path = Path(prompt_dir or PROMPT_DIR) / f"{name}.{version}.md"
    if not path.exists():
        raise PromptError(f"prompt not found: {path.name}")
    text = path.read_text(encoding="utf-8-sig")
    return {"name": name, "version": version, "sha": hashlib.sha256(text.encode()).hexdigest()[:12], "text": text}


def render(template, values):
    """{{name}} を一括置換。未指定のプレースホルダはエラー。差し込んだ値は再展開しない。"""
    missing = {m for m in _PH.findall(template) if m not in values}
    if missing:
        raise PromptError(f"missing values for placeholders: {sorted(missing)}")
    return _PH.sub(lambda m: str(values[m.group(1)]), template)
