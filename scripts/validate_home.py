"""Validate the structural integrity of the RYZM homepage."""
import argparse
from collections import Counter
from pathlib import Path
import re

import html5lib


def check(condition, message):
    if not condition:
        raise ValueError(message)


def validate_document(document):
    check(document.lstrip().lower().startswith("<!doctype html>"), "missing HTML doctype")
    check(document.rstrip().lower().endswith("</html>"), "missing closing </html>")
    check("earlier output omitted" not in document, "truncated output marker")
    parser = html5lib.HTMLParser(namespaceHTMLElements=False)
    # html5lib 1.1 flags the existing raw Google Fonts query separators.
    # Normalize those two separators only in the parser input.
    parser_input = re.sub(
        r'(<link href="https://fonts\.googleapis\.com/css2\?[^"\r\n]+)(" rel="stylesheet">)',
        lambda match: match[1].replace("&family=", "&amp;family=").replace("&display=", "&amp;display=") + match[2],
        document,
    )
    tree = parser.parse(parser_input)
    check(not parser.errors, f"HTML parsing errors: {parser.errors[:4]}")
    ids = Counter(element.get("id") for element in tree.iter() if "id" in element.attrib)
    check(not any(count > 1 for count in ids.values()), "duplicate HTML ID")
    breaking = tree.find(".//*[@id='breaking']")
    check(breaking is not None, "missing Breaking section")
    lists = [node for node in breaking.iter("ul") if node.get("class") == "breaking-list"]
    check(len(lists) == 1, "expected one Breaking list inside #breaking")
    feed_section = tree.find(".//*[@id='feed']")
    check(feed_section is not None, "missing Feed section")
    feeds = [node for node in feed_section.iter("div") if node.get("class") == "feed"]
    check(len(feeds) == 1, "expected one Feed list inside #feed")
    return lists[0], feeds[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page", type=Path)
    args = parser.parse_args()
    try:
        validate_document(args.page.read_bytes().decode("utf-8"))
    except (ValueError, OSError) as error:
        parser.exit(1, f"Error: {error}\n")
