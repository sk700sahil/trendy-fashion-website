"""Check public asset references and repository contact/secret hygiene before commit."""
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
TEXT_SUFFIXES = {".html", ".css", ".js", ".json", ".jsonc", ".md", ".csv", ".sql", ".py", ".toml", ".txt", ".yml", ".yaml"}


class References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []
        self.images_without_alt = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "img" and not attrs.get("alt"):
            self.images_without_alt += 1
        for attribute in ("src", "href"):
            if attrs.get(attribute):
                self.references.append(attrs[attribute])


def audit():
    issues = []
    pages = list(PUBLIC.rglob("*.html"))
    if not pages:
        issues.append("No public HTML pages exist.")
    for page in pages:
        parser = References()
        parser.feed(page.read_text(encoding="utf-8"))
        if parser.images_without_alt:
            issues.append(f"Missing image alt text: {page.relative_to(ROOT)}")
        for reference in parser.references:
            url = urlsplit(reference)
            if url.scheme or url.netloc or not url.path:
                continue
            path = unquote(url.path)
            target = PUBLIC / path.lstrip("/") if path.startswith("/") else page.parent / path
            if target.is_dir():
                target /= "index.html"
            if not target.is_file():
                issues.append(f"Broken reference in {page.relative_to(ROOT)}: {reference}")
    filenames = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
    ).decode("utf-8").split("\0")
    email_pattern = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
    phone_pattern = re.compile(r"(?<!\d)(?:\+91[ -]?)?[6-9]\d{4}[ -]?\d{5}(?!\d)")
    secret_pattern = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")
    for filename in filenames:
        path = ROOT / filename
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        emails = email_pattern.findall(content)
        if any(not (email.endswith("@example.com") or email.endswith("@users.noreply.github.com")) for email in emails):
            issues.append(f"Non-demo email address found: {filename}")
        # Numeric lockfile digests/URLs aren't contact details; inspect public-facing text.
        if path.suffix in {".html", ".md", ".csv"} and phone_pattern.search(content):
            issues.append(f"Possible real phone number found: {filename}")
        if secret_pattern.search(content):
            issues.append(f"Possible credential found: {filename}")
    if issues:
        print("\n".join(issues))
        raise SystemExit(1)
    print(f"PASS: {len(pages)} public HTML pages, local asset references, alt text, and repository contact/secret scan.")


if __name__ == "__main__":
    audit()
