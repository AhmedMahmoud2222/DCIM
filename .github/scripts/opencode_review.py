#!/usr/bin/env python3
"""Independent reviewer routing, separation checks and review publication (review v2.0).

Enforces IMPLEMENTER_REVIEWER_SEPARATION (registry key `agent_separation`):
  - the implementer is read from GitHub-verified commit trailers written by the publisher
    (OpenCode-Model / OpenCode-Session), authored by the dedicated App bot;
  - the reviewer must be a different approved model AND a different agent session;
  - the reviewer is the strongest model with recorded, reproducible evaluation evidence in
    the version-controlled capability matrix (speed and price are never inputs);
  - with no eligible reviewer the result is REVIEW_BLOCKED;
  - a review is a COMMENT-only PR review tied to the exact commit it covers, never an
    approval, and any later commit makes it STALE. External acceptance (Codex or Jules)
    stays required.

Subcommands (all inputs are trusted workflow values or API data):
  prepare   fetch PR, diff and commits; write REVIEW inputs; block on confidentiality
  select    choose the reviewer model (writes review_blocked / model / reason)
  check     verify model and session separation after the reviewer ran
  verify-readonly  fail if the reviewer changed anything in its sandbox
  publish   validate the reviewer's output and post the COMMENT review
  status    print CURRENT / STALE / NONE for the PR head
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import opencode_router as router  # noqa: E402
import opencode_sandbox as sandbox  # noqa: E402

DIMENSIONS = ('code_reasoning_correctness', 'security_vulnerability_identification',
              'database_transaction_safety', 'test_quality_regression_detection',
              'cross_module_dependency_analysis', 'complex_change_inspection')
EVALUATION_KEYS = {'suite', 'suite_version', 'date', 'run_url', 'results_sha256', 'scores'}
SEVERITIES = ('critical', 'high', 'medium', 'low', 'info')
BLOCKING = ('critical', 'high')
MAX_DIFF_BYTES = 1_500_000
MAX_FINDINGS = 50
SESSION_RE = re.compile(r'^[A-Za-z0-9_-]{6,80}$')
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
TRAILER_RE = re.compile(r'^OpenCode-(Model|Session|Run|Mode):\s*(\S.*?)\s*$', re.M)
MARKER_RE = re.compile(r'\A<!-- opencode-review:(\{.*?\}) -->')
PATH_RE = re.compile(r'^[A-Za-z0-9._/@+-]{1,300}$')


class ReviewBlocked(Exception):
    """No independent review is possible (REVIEW_BLOCKED)."""


class SeparationError(Exception):
    pass


class ReviewError(Exception):
    pass


# ---------------------------------------------------------------- capability matrix

def validate_matrix(matrix: object) -> dict:
    if not isinstance(matrix, dict) or not isinstance(matrix.get('dimensions'), dict):
        raise ReviewBlocked('capability matrix is malformed')
    weights = matrix['dimensions']
    if set(weights) != set(DIMENSIONS) or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0 for v in weights.values()) \
            or abs(sum(weights.values()) - 1.0) > 1e-9:
        raise ReviewBlocked('capability matrix weights must cover exactly the required dimensions and sum to 1')
    floor = matrix.get('minimum_dimension_score')
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not 0 <= floor <= 1:
        raise ReviewBlocked('capability matrix minimum_dimension_score is invalid')
    if not isinstance(matrix.get('models'), dict):
        raise ReviewBlocked('capability matrix has no models section')
    return matrix


def capability(matrix: dict, model: str) -> tuple[float, float, str]:
    """Return (weighted score, weakest dimension, '') or (0, 0, reason) when evidence is unusable."""
    evaluation = (matrix['models'].get(model) or {}).get('evaluation')
    if not isinstance(evaluation, dict) or set(evaluation) != EVALUATION_KEYS:
        return 0.0, 0.0, 'no verified capability evidence'
    for key in ('suite', 'suite_version', 'date', 'run_url'):
        if not isinstance(evaluation[key], str) or not evaluation[key].strip():
            return 0.0, 0.0, f'evaluation record incomplete ({key})'
    if not evaluation['run_url'].startswith('https://') or not isinstance(evaluation['results_sha256'], str) \
            or not re.fullmatch(r'[0-9a-f]{64}', evaluation['results_sha256']):
        return 0.0, 0.0, 'evaluation record is not reproducible (run_url or results_sha256)'
    scores = evaluation['scores']
    if not isinstance(scores, dict) or set(scores) != set(DIMENSIONS) or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 for v in scores.values()):
        return 0.0, 0.0, 'evaluation scores incomplete or out of range'
    weakest = min(scores.values())
    if weakest < matrix['minimum_dimension_score']:
        return 0.0, weakest, f'capability not demonstrated in every dimension (weakest {weakest})'
    return sum(matrix['dimensions'][d] * scores[d] for d in DIMENSIONS), weakest, ''


# ---------------------------------------------------------------- implementer records

def implementer_records(commits: list[dict], bot_login: str) -> list[dict]:
    """Implementation records from GitHub-verified commits authored by the App bot."""
    records = []
    for commit in commits if isinstance(commits, list) else []:
        verification = (commit.get('commit') or {}).get('verification') or {}
        author = ((commit.get('author') or {}).get('login') or '').lower()
        if verification.get('verified') is not True or verification.get('reason') != 'valid' \
                or author != bot_login.lower():
            continue
        trailers = {k: v for k, v in TRAILER_RE.findall((commit.get('commit') or {}).get('message', ''))}
        if trailers.get('Mode') != 'implement' or 'Model' not in trailers:
            continue
        records.append({'commit': commit.get('sha', ''), 'model': router.normalize(trailers['Model']),
                        'session': trailers.get('Session', ''), 'run': trailers.get('Run', '')})
    return records


# ---------------------------------------------------------------- routing and separation

def select_reviewer(registry: dict, matrix: dict, records: list[dict], available: set[str]) -> dict:
    router.load_registry(registry)  # also enforces the agent_separation policy
    validate_matrix(matrix)
    implementers = {r['model'] for r in records}
    ranked, excluded = [], {}
    for model in router.APPROVED_MODELS:
        reasons = []
        if model in implementers:
            reasons.append('implemented this branch or PR')
        reasons += router.blockers(registry['models'][model])
        if model not in available:
            reasons.append('not in live catalog')
        score, weakest, why = capability(matrix, model)
        if why:
            reasons.append(why)
        if reasons:
            excluded[model] = reasons
        else:
            ranked.append((-score, -weakest, model))
    if not ranked:
        raise ReviewBlocked('no eligible independent reviewer: ' + '; '.join(
            f'{m}: {", ".join(r)}' for m, r in sorted(excluded.items())))
    ranked.sort()
    best = ranked[0]
    return {'model': best[2], 'score': round(-best[0], 6),
            'reason': f'highest verified weighted capability {-best[0]:.3f} among {len(ranked)} eligible; '
                      f'excluded {len(excluded)}', 'excluded': excluded}


def check_separation(registry: dict, records: list[dict], reviewer_model: str, reviewer_session: str) -> None:
    policy = router.separation_policy(registry)
    model = router.normalize(reviewer_model)
    if model not in router.APPROVED_MODELS:
        raise SeparationError('reviewer is not an approved model')
    if policy['require_distinct_models'] and model in {r['model'] for r in records}:
        raise SeparationError('reviewer model implemented this branch or PR')
    if policy['require_distinct_sessions']:
        if not SESSION_RE.match(reviewer_session or ''):
            raise SeparationError('reviewer session identity is missing')
        if reviewer_session in {r['session'] for r in records if r['session']}:
            raise SeparationError('reviewer reused an implementer session')


# ---------------------------------------------------------------- review output

def clean(text: object, limit: int) -> str:
    text = text if isinstance(text, str) else ''
    text = re.sub(r'[^\S\n]+', ' ', re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text)).strip()[:limit]
    text = re.sub(r'@(?=[A-Za-z0-9_-])', '@​', text)           # no mentions
    text = text.replace('<!--', '<!​--').replace('-->', '-​->')  # no hidden markers
    return re.sub(r'(?m)^(\s*)/(?=\w)', '\\1/​', text)           # no slash commands


def parse_review(text: str) -> dict:
    blocks = re.findall(r'```json\s*(\{.*?\})\s*```', text or '', re.S)
    candidate = blocks[-1] if blocks else (text or '').strip()
    try:
        data = json.loads(candidate)
    except ValueError:
        raise ReviewError('review output is not valid JSON') from None
    if not isinstance(data, dict) or data.get('verdict') not in ('no_blocking_findings', 'blocking_findings'):
        raise ReviewError('review verdict missing or not an allowed value (approval is never allowed)')
    findings = data.get('findings')
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        raise ReviewError('findings missing or too many')
    out = []
    for item in findings:
        if not isinstance(item, dict) or item.get('severity') not in SEVERITIES:
            raise ReviewError('finding has an invalid severity')
        files = item.get('files')
        if not isinstance(files, list) or len(files) > 20 or not all(
                isinstance(f, str) and PATH_RE.match(f) and '..' not in f for f in files):
            raise ReviewError('finding has invalid affected files')
        entry = {'severity': item['severity'], 'title': clean(item.get('title'), 200), 'files': list(files),
                 'evidence': clean(item.get('evidence'), 2000), 'recommendation': clean(item.get('recommendation'), 1000)}
        if not entry['title'] or not entry['recommendation'] or (
                entry['severity'] in BLOCKING and not entry['evidence']):
            raise ReviewError('finding lacks title, recommendation or (for critical/high) evidence')
        out.append(entry)
    verification = []
    for item in (data.get('verification') or [])[:20]:
        if isinstance(item, dict):
            verification.append({'command': clean(item.get('command'), 300), 'result': clean(item.get('result'), 500)})
    blocking = data['verdict'] == 'blocking_findings' or any(f['severity'] in BLOCKING for f in out)
    out.sort(key=lambda f: SEVERITIES.index(f['severity']))
    return {'verdict': 'BLOCKING_FINDINGS' if blocking else 'NO_BLOCKING_FINDINGS',
            'summary': clean(data.get('summary'), 2000), 'findings': out, 'verification': verification}


def render_review(review: dict, ctx: dict) -> str:
    marker = {'reviewed_commit': ctx['commit'], 'base_commit': ctx['base_commit'],
              'stale_on_arrival': bool(ctx.get('stale_on_arrival')), 'reviewer_model': ctx['reviewer_model'],
              'reviewer_session': ctx['reviewer_session'],
              'implementer_models': sorted({r['model'] for r in ctx['records']}),
              'implementer_sessions': sorted({r['session'] for r in ctx['records'] if r['session']}),
              'verdict': review['verdict'], 'external_acceptance': 'REQUIRED',
              'run_url': ctx.get('run_url', '')}
    lines = [f"<!-- opencode-review:{json.dumps(marker, separators=(',', ':'))} -->",
             f"## OpenCode independent review: {review['verdict']}", '',
             *(['- **STALE ON ARRIVAL**: the pull request head or base moved before this review was posted; '
                'it describes the commits below, not the current PR state.'] if ctx.get('stale_on_arrival') else []),
             f"- Reviewed commits: base `{ctx['base_commit']}`, head `{ctx['commit']}` (valid for exactly these "
             'two revisions; a change to either makes it stale)',
             f"- Reviewer: `{ctx['reviewer_model']}`, session `{ctx['reviewer_session']}` (read-only sandbox)"]
    for record in ctx['records']:
        lines.append(f"- Implementer: `{record['model']}`, session `{record['session'] or 'unrecorded'}`, "
                     f"commit `{record['commit'][:12]}`, branch `{ctx['branch']}`")
    if not ctx['records']:
        lines.append('- Implementer: no OpenCode implementation commit found on this PR')
    lines += ['- This review is not an approval. External acceptance by Codex or Jules is still required before merge.',
              f"- Run: {ctx['run_url']}", '', review['summary'], '']
    if not review['findings']:
        lines.append('No findings reported.')
    for index, finding in enumerate(review['findings'], 1):
        lines += [f"### {index}. [{finding['severity'].upper()}] {finding['title']}",
                  f"Files: {', '.join('`' + f + '`' for f in finding['files']) or 'n/a'}",
                  f"Evidence: {finding['evidence'] or 'n/a (not applicable at this severity)'}",
                  f"Recommendation: {finding['recommendation']}", '']
    lines.append('### Verification evidence (reported by the reviewer, not re-run by the publisher)')
    lines += [f"- `{v['command']}`: {v['result']}" for v in review['verification']] or ['- none reported']
    return '\n'.join(lines)


def acceptance_status(reviews: list[dict], head_sha: str, bot_login: str, base_sha: str | None = None) -> dict:
    """CURRENT only when the newest OpenCode review covers exactly head_sha (and base_sha when
    given) and was not stale when posted. Never 'accepted'."""
    best = None
    for review in reviews if isinstance(reviews, list) else []:
        user = review.get('user') or {}
        if (user.get('login') or '').lower() != bot_login.lower():
            continue
        match = MARKER_RE.match(review.get('body') or '')
        if not match:
            continue
        try:
            marker = json.loads(match.group(1))
        except ValueError:
            continue
        if marker.get('reviewed_commit') != review.get('commit_id') or not SHA_RE.match(str(marker.get('reviewed_commit'))):
            continue  # GitHub's recorded commit must agree with the marker
        stamp = review.get('submitted_at') or ''
        if best is None or stamp >= best[0]:
            best = (stamp, marker)
    if best is None:
        return {'status': 'NONE', 'accepted': False, 'external_acceptance': 'REQUIRED'}
    marker = best[1]
    current = (marker['reviewed_commit'] == head_sha and not marker.get('stale_on_arrival')
               and (base_sha is None or marker.get('base_commit') == base_sha))
    status = 'CURRENT' if current else 'STALE'
    return {'status': status, 'reviewed_commit': marker['reviewed_commit'], 'base_commit': marker.get('base_commit'),
            'verdict': marker.get('verdict'),
            'accepted': False, 'external_acceptance': 'REQUIRED'}


def assert_read_only(manifest: dict, diff_sha256: str) -> None:
    """The reviewer's tree must equal the sanitized baseline plus the untouched diff file."""
    if manifest.get('unsupported'):
        raise ReviewError('reviewer left unsupported file types in the sandbox')
    for change in manifest.get('changes', []):
        if not (change.get('path') == 'REVIEW/diff.patch' and change.get('op') == 'add'
                and change.get('sha256') == diff_sha256):
            raise ReviewError(f"reviewer modified the sandbox: {str(change.get('path'))[:80]}")


# ---------------------------------------------------------------- inputs and GitHub access

def gh_json(path: str, accept: str | None = None):
    cmd = ['gh', 'api', *(['-H', f'Accept: {accept}'] if accept else []), path]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return out if accept else json.loads(out)


_ESCAPES = {'a': 7, 'b': 8, 'f': 12, 'n': 10, 'r': 13, 't': 9, 'v': 11, '\\': 92, '"': 34}


def unquote_git_path(token: str) -> str:
    """Decode a git C-style quoted path ("..." with \\ooo octal and \\n style escapes)."""
    if not (len(token) >= 2 and token[0] == '"' and token[-1] == '"'):
        return token
    raw, out, i = token[1:-1], bytearray(), 0
    while i < len(raw):
        ch = raw[i]
        if ch != '\\':
            out += ch.encode('utf-8')
            i += 1
            continue
        i += 1
        if i < len(raw) and raw[i] in '0123':
            digits = raw[i:i + 3]
            if len(digits) == 3 and all(d in '01234567' for d in digits):
                out.append(int(digits, 8))
                i += 3
                continue
        if i < len(raw) and raw[i] in _ESCAPES:
            out.append(_ESCAPES[raw[i]])
            i += 1
            continue
        raise ReviewBlocked('diff contains an unrecognised path escape')
    return out.decode('utf-8', 'replace')


def _split_quoted_pair(rest: str) -> list[str]:
    tokens, i = [], 0
    while i < len(rest):
        if rest[i] == ' ':
            i += 1
        elif rest[i] == '"':
            j = i + 1
            while j < len(rest) and not (rest[j] == '"' and rest[j - 1] != '\\'):
                j += 1
            tokens.append(rest[i:j + 1])
            i = j + 1
        else:
            j = rest.find(' ', i)
            j = len(rest) if j < 0 else j
            tokens.append(rest[i:j])
            i = j
    return tokens


def diff_paths(diff: str) -> set[str]:
    """Every path a unified git diff names, decoded; quoted and unquoted forms both handled."""
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith('diff --git '):
            rest = line[len('diff --git '):]
            if rest.startswith('"') or '"' in rest:
                for token in _split_quoted_pair(rest):
                    path = unquote_git_path(token)
                    paths.add(path[2:] if path[:2] in ('a/', 'b/') else path)
            elif rest.startswith('a/') and ' b/' in rest and len(rest) % 2 == 1:
                half = (len(rest) - 5) // 2          # 'a/' + N + ' b/' + N when both names are equal
                if rest[2 + half:5 + half] == ' b/' and rest[2:2 + half] == rest[5 + half:]:
                    paths.add(rest[2:2 + half])
        elif line.startswith(('rename from ', 'rename to ', 'copy from ', 'copy to ')):
            paths.add(unquote_git_path(line.split(' ', 2)[2]))
        elif line.startswith(('--- ', '+++ ')):
            token = line[4:].split('\t')[0]
            token = unquote_git_path(token)
            if token != '/dev/null':
                paths.add(token[2:] if token[:2] in ('a/', 'b/') else token)
    return paths


def prepare_inputs(fetch, repo: str, number: str, head_sha: str, base_sha: str, bot_login: str,
                   out_dir: str) -> dict:
    """Collect review material from the two immutable revisions captured at authorization."""
    if not (SHA_RE.match(head_sha) and SHA_RE.match(base_sha)):
        raise ReviewBlocked('trusted base or head SHA is missing')
    compare = f'repos/{repo}/compare/{base_sha}...{head_sha}'
    diff = fetch(compare, 'application/vnd.github.diff')
    if len(diff.encode()) > MAX_DIFF_BYTES:
        raise ReviewBlocked('diff is too large to inspect in full')
    commits, files, page = [], [], 1
    while True:
        chunk = fetch(f'{compare}?per_page=100&page={page}')
        commits += chunk.get('commits', [])
        if page == 1:
            files = chunk.get('files', [])
            total = chunk.get('total_commits', len(commits))
            if chunk.get('merge_base_commit') is None or len(files) >= 300:
                raise ReviewBlocked('file list may be truncated or the revisions are unrelated')
        if len(commits) >= total or page >= 10:
            break
        page += 1
    if len(commits) < total:
        raise ReviewBlocked('too many commits to establish implementer records')
    api_names = {f['filename'] for f in files} | {f['previous_filename'] for f in files if f.get('previous_filename')}
    named = diff_paths(diff)
    if not named <= api_names:
        raise ReviewBlocked('diff names paths that GitHub does not list for these revisions; refusing to guess')
    flagged = [f for f in api_names | named if sandbox.is_sensitive_name(f) or sandbox.is_untrusted_config(f)]
    if flagged:
        raise ReviewBlocked('diff touches credential-like or agent-configuration files: ' + ', '.join(sorted(flagged)[:5]))
    if sandbox.SECRET_RE.search(diff):
        raise ReviewBlocked('diff contains secret-like content; confidentiality cannot be established')
    records = implementer_records(commits, bot_login)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, 'diff.patch'), 'w', encoding='utf-8') as handle:
        handle.write(diff)
    with open(os.path.join(out_dir, 'implementers.json'), 'w', encoding='utf-8') as handle:
        json.dump(records, handle)
    return {'files': sorted(api_names), 'records': records, 'base': base_sha, 'head': head_sha}


def load_json(path: str):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def emit(**values) -> None:
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as handle:
            for key, value in values.items():
                handle.write(f'{key}={value}\n')


def one_line(text: str) -> str:
    return re.sub(r'[^A-Za-z0-9 .,:;()/_-]+', ' ', text)[:300]


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ''
    env = os.environ
    try:
        if command == 'prepare':
            result = prepare_inputs(gh_json, env['GITHUB_REPOSITORY'], env['TARGET_NUMBER'], env['AUTHORIZED_SHA'],
                                    env['BASE_SHA'], env['EXPECT_BOT_LOGIN'], argv[2])
            emit(review_blocked='false')
            print(f"review inputs ready: {len(result['files'])} files, {len(result['records'])} implementer record(s)")
            return 0
        if command == 'select':
            registry = load_json(argv[2])
            matrix = load_json(argv[3])
            available = router.parse_available(load_json(argv[4]))
            records = load_json(argv[5])
            if router.SENSITIVE_RE.search(env.get('TASK', '')):
                raise ReviewBlocked('review focus text looks like secret or confidential material')
            chosen = select_reviewer(registry, matrix, records, available)
            emit(review_blocked='false', model=chosen['model'], reason=one_line(chosen['reason']))
            print(f"reviewer={chosen['model']} score={chosen['score']} ({chosen['reason']})")
            return 0
        if command == 'check':
            registry = load_json(argv[2])
            records = load_json(argv[3])
            check_separation(registry, records, env['REVIEWER_MODEL'], env['REVIEWER_SESSION'])
            print('separation verified: distinct model and session')
            return 0
        if command == 'verify-readonly':
            manifest = load_json(argv[2])
            with open(argv[3], 'rb') as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            assert_read_only(manifest, digest)
            print('reviewer sandbox unmodified')
            return 0
        if command == 'publish':
            return publish_main(argv)
        if command == 'status':
            reviews = gh_json(f"repos/{env['GITHUB_REPOSITORY']}/pulls/{env['TARGET_NUMBER']}/reviews?per_page=100")
            pr = gh_json(f"repos/{env['GITHUB_REPOSITORY']}/pulls/{env['TARGET_NUMBER']}")
            print(json.dumps(acceptance_status(reviews, pr['head']['sha'], env['EXPECT_BOT_LOGIN'], pr['base']['sha'])))
            return 0
    except ReviewBlocked as error:
        emit(review_blocked='true', block_reason=one_line(str(error)))
        print(f'REVIEW_BLOCKED: {error}')
        return 0
    except (SeparationError, ReviewError, KeyError, OSError, ValueError, subprocess.CalledProcessError,
            router.RoutingError) as error:
        print(f'::error::OpenCode review stopped: {type(error).__name__}: {one_line(str(error))}')
        return 1
    print(__doc__, file=sys.stderr)
    return 2


def publish_main(argv: list[str]) -> int:
    import opencode_publish as pub  # noqa: PLC0415  (only the publish job needs it)
    env = os.environ
    repo, number, sha = env['GITHUB_REPOSITORY'], env['TARGET_NUMBER'], env['AUTHORIZED_SHA']
    api = pub.Api(env['GH_PUBLISH_TOKEN'], repo)
    registry = load_json(argv[2])
    with open(os.path.join(argv[3], 'review-text.txt'), encoding='utf-8') as handle:
        text = handle.read()
    base_sha = env['BASE_SHA']
    pr = api.call('GET', f'pulls/{number}')
    head, pr_base = pr.get('head') or {}, pr.get('base') or {}
    if ((head.get('repo') or {}).get('full_name') or '').lower() != repo.lower() \
            or ((pr_base.get('repo') or {}).get('full_name') or '').lower() != repo.lower():
        raise ReviewError('pull request repositories changed; review not posted')
    # A moved head or base, or a closed PR, does not discard the review: it is posted bound to the
    # revisions it examined and marked stale, so nobody mistakes it for the current state.
    stale_on_arrival = (head.get('sha') != sha or pr_base.get('sha') != base_sha
                        or pr.get('state') != 'open' or bool(pr.get('merged')))
    commits = []
    for page in range(1, 6):  # up to 500 commits; a longer history cannot be separated safely
        chunk = api.call('GET', f'pulls/{number}/commits?per_page=100&page={page}')
        commits += chunk
        if len(chunk) < 100:
            break
    else:
        raise ReviewError('pull request has too many commits to establish implementer records')
    records = implementer_records(commits, env['EXPECT_BOT_LOGIN'])
    check_separation(registry, records, env['REVIEWER_MODEL'], env['REVIEWER_SESSION'])
    review = parse_review(text)
    body = render_review(review, {
        'commit': sha, 'reviewer_model': env['REVIEWER_MODEL'], 'reviewer_session': env['REVIEWER_SESSION'],
        'records': records, 'branch': head.get('ref', ''), 'run_url': env['RUN_URL'],
        'base_commit': base_sha, 'stale_on_arrival': stale_on_arrival})
    # COMMENT only: this publisher never submits an approving or change-requesting review and never merges.
    posted = api.call('POST', f'pulls/{number}/reviews', {'commit_id': sha, 'body': body, 'event': 'COMMENT'})
    emit(review_verdict=review['verdict'], review_stale=str(stale_on_arrival).lower(),
         review_id=posted.get('id', ''))
    print(f"review posted for {sha[:12]}: {review['verdict']}, {len(review['findings'])} finding(s)"
          + (' [STALE ON ARRIVAL]' if stale_on_arrival else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
