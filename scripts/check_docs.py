"""Validate tracked Markdown links offline, without third-party dependencies."""
from __future__ import annotations

import argparse
from collections import Counter
import html
import json
from pathlib import Path
import posixpath
import re
import subprocess
from urllib.parse import unquote, urlsplit


# Bundled tool documentation is maintained upstream, not by this repository.
EXCLUDED_PREFIXES = ('.claude/skills/',)


def visible_markdown(text: str) -> str:
    """Blank code blocks/comments while preserving offsets and line numbers."""
    lines = text.splitlines(keepends=True)
    fence = None
    for i, line in enumerate(lines):
        match = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if fence:
            lines[i] = re.sub(r'[^\n]', ' ', line)
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence):
                fence = None
        elif match:
            fence = match[1]
            lines[i] = re.sub(r'[^\n]', ' ', line)
        elif line.startswith(('    ', '\t')):
            lines[i] = re.sub(r'[^\n]', ' ', line)
    return re.sub(r'<!--[\s\S]*?-->', lambda m: re.sub(r'[^\n]', ' ', m[0]), ''.join(lines))


def anchors(text: str) -> set[str]:
    text = visible_markdown(text)
    result = set(re.findall(r'\b(?:id|name)\s*=\s*[\"\']([^\"\']+)[\"\']', text, re.I))
    counts: Counter[str] = Counter()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = re.match(r'^ {0,3}#{1,6}\s+(.+?)(?:\s+#+)?\s*$', line)
        heading = match[1] if match else None
        if not heading and i + 1 < len(lines) and re.fullmatch(r' {0,3}(?:=+|-+)\s*', lines[i + 1]) and line.strip():
            heading = line.strip()
        if heading is None:
            continue
        heading = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', heading)
        heading = html.unescape(re.sub(r'<[^>]*>', '', heading))
        slug = re.sub(r'[^\w\- ]', '', heading.lower()).replace(' ', '-')
        candidate = slug
        while candidate in result:
            counts[slug] += 1
            candidate = f'{slug}-{counts[slug]}'
        result.add(candidate)
    return result


def references(text: str):
    """Yield (offset, URL); recognize inline, full/collapsed/shortcut references and HTML."""
    text = visible_markdown(text)
    # Code spans are not navigable links.
    text = re.sub(r'(`+)([\s\S]*?)\1', lambda m: re.sub(r'[^\n]', ' ', m[0]), text)
    definitions = {}
    definition_ranges = []
    for match in re.finditer(r'^ {0,3}\[([^\]\n]+)\]:\s*(<[^>\n]+>|\S+)', text, re.M):
        definitions[' '.join(match[1].lower().split())] = match[2].strip('<>')
        definition_ranges.append(match.span())
        yield match.start(), match[2].strip('<>')
    for start, end in reversed(definition_ranges):
        text = text[:start] + ' ' * (end - start) + text[end:]
    pattern = re.compile(r'!?\[([^\]\n]*(?:\[[^\]\n]*\][^\]\n]*)*)\]')
    cursor = 0
    while match := pattern.search(text, cursor):
        cursor = match.end()
        if match.start() and text[match.start() - 1] == '\\':
            continue
        if text[cursor:cursor + 1] == '(':
            begin = cursor + 1
            end = begin
            depth = 1
            while end < len(text) and depth:
                if text[end] == '\\':
                    end += 2
                    continue
                depth += (text[end] == '(') - (text[end] == ')')
                end += 1
            if depth == 0:
                value = text[begin:end - 1].strip()
                if value.startswith('<'):
                    value = value[1:value.find('>')]
                else:
                    value = re.split(r'\s+[\"\']', value, maxsplit=1)[0]
                yield match.start(), re.sub(r'\\([() ])', r'\1', value)
                cursor = end
        else:
            suffix = re.match(r'\[([^\]\n]*)\]', text[cursor:])
            label = suffix[1] or match[1] if suffix else match[1]
            key = ' '.join(label.lower().split())
            if key in definitions:
                yield match.start(), definitions[key]
            elif suffix:
                yield match.start(), 'undefined-reference:' + label
            if suffix:
                cursor += suffix.end()
    for match in re.finditer(r'<[^>]+\b(?:href|src)\s*=\s*[\"\']([^\"\']+)[\"\'][^>]*>', text, re.I):
        yield match.start(), html.unescape(match[1])


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'], check=True, capture_output=True)
    return sorted(set(result.stdout.decode('utf-8').rstrip('\0').split('\0')))


def path_references(root: Path, paths: list[str]) -> list[dict]:
    """Inventory literal Markdown paths, including code examples and source comments.

    These are not necessarily navigation (tests deliberately name nonexistent files),
    so expose them separately for review rather than pretending they are hyperlinks.
    """
    tracked = set(paths)
    mapping_file = root / 'docs/documentation-moves.json'
    moves = json.loads(mapping_file.read_text(encoding='utf-8-sig')) if mapping_file.exists() else {}
    findings = []
    pattern = re.compile(r'(?<![\w./-])(?:[\w.-]+/)*[\w.-]+\.[mM][dD](?:#[\w-]+)?(?![\w.-])')
    for path in paths:
        if path.startswith(EXCLUDED_PREFIXES) or path == 'docs/documentation-moves.json':
            continue
        try:
            content = (root / path).read_text(encoding='utf-8-sig')
        except (UnicodeError, OSError):
            continue
        for number, line in enumerate(content.splitlines(), 1):
            # Remote URLs are preserved, not interpreted as worktree paths.
            line = re.sub(r'(?:https?|git)://[^\s<>\"\']+', '', line)
            for match in pattern.finditer(line):
                target = match[0].split('#', 1)[0]
                relative = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
                if target in tracked or relative in tracked:
                    continue
                replacement = moves.get(target)
                findings.append(dict(path=path, line=number, target=target,
                                     reason='relocated literal path' if replacement else 'unresolved literal path',
                                     replacement=replacement))
    return findings


def audit(root: Path, paths: list[str]) -> dict:
    root = root.resolve()
    tracked = set(paths)
    markdown = [p for p in paths if p.lower().endswith('.md')]
    excluded = [p for p in markdown if p.startswith(EXCLUDED_PREFIXES)]
    documents = [p for p in markdown if p not in excluded]
    problems = []
    checked = 0
    external = 0
    anchor_cache = {}
    for path in documents:
        source = root / path
        if not source.is_file():
            problems.append(dict(path=path, line=1, target=path, reason='tracked document missing'))
            continue
        content = source.read_text(encoding='utf-8-sig')
        for offset, target in references(content):
            error = None
            if target.startswith('undefined-reference:'):
                error = 'undefined reference label'
            else:
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc:
                    external += 1
                    continue
                checked += 1
                local = unquote(parsed.path)
                destination = posixpath.normpath(local.lstrip('/') if local.startswith('/') else posixpath.join(posixpath.dirname(path), local)) if local else path
                if destination == '..' or destination.startswith('../'):
                    error = 'path escapes repository'
                elif destination not in tracked and not any(p.startswith(destination.rstrip('/') + '/') for p in tracked):
                    error = 'missing tracked target (path and case must match)'
                elif not (root / destination).exists():
                    error = 'target missing from worktree'
                elif parsed.fragment and destination.lower().endswith('.md'):
                    if destination not in anchor_cache:
                        anchor_cache[destination] = anchors((root / destination).read_text(encoding='utf-8-sig'))
                    if unquote(parsed.fragment) not in anchor_cache[destination]:
                        error = 'invalid Markdown anchor'
            if error:
                problems.append(dict(path=path, line=content.count('\n', 0, offset) + 1, target=target, reason=error))
    return dict(markdown_files=len(markdown), checked_documents=len(documents), excluded_documents=excluded,
                local_references_checked=checked, external_references_skipped=external, errors=problems)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--json', action='store_true', help='emit machine-readable results')
    parser.add_argument('--path-report', action='store_true', help='also inventory unresolved literal .md paths in all tracked text')
    args = parser.parse_args()
    result = audit(args.root, tracked_files(args.root))
    if args.path_report:
        result['literal_path_findings'] = path_references(args.root, tracked_files(args.root))
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        for error in result['errors']:
            print(f"{error['path']}:{error['line']}: {error['reason']}: {error['target']}")
        print(f"Checked {result['checked_documents']} Markdown documents, {result['local_references_checked']} local references; "
              f"{len(result['excluded_documents'])} vendor documents excluded; {len(result['errors'])} errors.")
        for finding in result.get('literal_path_findings', []):
            print(f"REVIEW {finding['path']}:{finding['line']}: {finding['reason']}: {finding['target']}")
    return bool(result['errors'])


if __name__ == '__main__':
    raise SystemExit(main())
