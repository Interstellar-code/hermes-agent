from __future__ import annotations

import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


class WikiStore:
    CORE_FILES = ("SCHEMA.md", "index.md", "log.md")
    FOLDERS = ("raw", "entities", "concepts", "comparisons", "queries")

    def __init__(self, root: Path):
        self.root = Path(root)

    def ensure_structure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in self.FOLDERS:
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        schema = self.root / "SCHEMA.md"
        if not schema.exists():
            schema.write_text(
                "# Matrix Memory Schema\n\nPages live under topic folders and cross-reference with `[[wikilinks]]`.\n",
                encoding="utf-8",
            )
        index = self.root / "index.md"
        if not index.exists():
            index.write_text("# Matrix Memory Index\n\n", encoding="utf-8")
        log = self.root / "log.md"
        if not log.exists():
            log.write_text("# Matrix Memory Log\n\n", encoding="utf-8")
        self.refresh_index()

    def slugify(self, text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return slug or "untitled"

    def page_path(self, *, title: str, folder: str) -> str:
        return f"{folder}/{self.slugify(title)}.md"

    def _render_page(self, *, title: str, content: str, folder: str) -> str:
        now = datetime.now(timezone.utc).isoformat()
        return (
            "---\n"
            f"title: {title}\n"
            f"folder: {folder}\n"
            f"updated_at: {now}\n"
            "---\n\n"
            f"{content.strip()}\n"
        )

    def preview_page(self, *, title: str, content: str, folder: str) -> dict:
        rel = self.page_path(title=title, folder=folder)
        return {
            "path": rel,
            "bytes": len(content.encode("utf-8")),
            "links": self.extract_links(content),
        }

    def preview_ingest(self, *, source: str, title: str, content: str, folder: str) -> dict:
        chosen_title = title or Path(source).stem or "ingested-note"
        preview_content = content or f"Ingest from {source}"
        return self.preview_page(title=chosen_title, content=preview_content, folder=folder)

    def write_page(self, *, title: str, content: str, folder: str = "queries") -> str:
        rel = self.page_path(title=title, folder=folder)
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._render_page(title=title, content=content, folder=folder), encoding="utf-8")
        self.refresh_index()
        return rel

    def ingest(self, *, source: str, title: str, content: str, folder: str = "raw") -> tuple[str, str]:
        body = content
        if not body and source:
            if source.startswith(("http://", "https://")):
                with urllib.request.urlopen(source, timeout=10) as response:  # noqa: S310 - user requested URL ingest
                    body = response.read().decode("utf-8", errors="replace")
            else:
                body = Path(source).read_text(encoding="utf-8")
        chosen_title = title or Path(source).stem or "ingested-note"
        rel = self.write_page(title=chosen_title, content=body, folder=folder)
        return rel, self.read_page(rel)

    def read_page(self, relpath: str) -> str:
        return (self.root / relpath).read_text(encoding="utf-8")

    def delete_page(self, relpath: str) -> bool:
        path = self.root / relpath
        if not path.exists():
            return False
        path.unlink()
        self.refresh_index()
        return True

    def iter_pages(self) -> list[str]:
        pages: list[str] = []
        for folder in self.FOLDERS:
            base = self.root / folder
            if not base.exists():
                continue
            for path in sorted(base.rglob("*.md")):
                pages.append(str(path.relative_to(self.root)))
        return pages

    def list_pages(self, folder: str | None = None) -> list[str]:
        if folder:
            base = self.root / folder
            if not base.exists():
                return []
            return sorted(str(path.relative_to(self.root)) for path in base.rglob("*.md"))
        return self.iter_pages()

    def extract_links(self, text: str) -> list[str]:
        return [match.group(1).strip() for match in WIKILINK_RE.finditer(text)]

    def resolve_link(self, link: str) -> str | None:
        slug = self.slugify(link)
        for rel in self.iter_pages():
            if Path(rel).stem == slug:
                return rel
        return None

    def search_pages(self, query: str) -> list[dict]:
        needle = query.lower()
        results = []
        for rel in self.iter_pages():
            text = self.read_page(rel)
            if needle in rel.lower() or needle in text.lower():
                results.append({"path": rel, "title": Path(rel).stem, "links": self.extract_links(text)})
        return results

    def strip_frontmatter(self, text: str) -> str:
        return FRONTMATTER_RE.sub("", text, count=1)

    def chunks_for_page(self, relpath: str, content: str) -> list[dict]:
        body = self.strip_frontmatter(content)
        heading = Path(relpath).stem
        chunks: list[dict] = []
        current_heading = heading
        current_lines: list[str] = []
        for line in body.splitlines():
            if line.startswith("#"):
                if current_lines:
                    chunks.append({"heading": current_heading, "text": "\n".join(current_lines).strip()})
                    current_lines = []
                current_heading = line.lstrip("#").strip() or heading
            else:
                current_lines.append(line)
        if current_lines:
            chunks.append({"heading": current_heading, "text": "\n".join(current_lines).strip()})
        return [chunk for chunk in chunks if chunk["text"]]

    def page_tokens(self, relpath: str) -> set[str]:
        text = self.read_page(relpath).lower()
        return {token for token in re.findall(r"[a-z0-9]+", text) if len(token) > 2}

    def page_age_days(self, relpath: str) -> float:
        path = self.root / relpath
        return (datetime.now(timezone.utc).timestamp() - path.stat().st_mtime) / 86400.0

    def refresh_index(self) -> None:
        lines = ["# Matrix Memory Index", ""]
        for folder in self.FOLDERS:
            lines.append(f"## {folder}")
            pages = self.list_pages(folder)
            if pages:
                lines.extend(f"- [[{Path(page).stem}]] ({page})" for page in pages)
            else:
                lines.append("- (empty)")
            lines.append("")
        (self.root / "index.md").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
