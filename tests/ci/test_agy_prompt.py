import importlib.util
import pathlib

_p = pathlib.Path(__file__).resolve().parents[2] / '.github/scripts/agy_prompt.py'
_s = importlib.util.spec_from_file_location('agy_prompt', _p)
agy = importlib.util.module_from_spec(_s)
_s.loader.exec_module(agy)


def test_command_must_start_a_line():
    assert agy.parse_command('@agy review', True)[0]
    assert not agy.parse_command('please ask @agy review', True)[0]
    assert not agy.parse_command('> quoted @agy review', True)[0]
    assert agy.parse_command('text\n  @AGY review focus on rbac', True) == (True, None, 'focus on rbac')


def test_issue_comment_needs_pr_number():
    assert not agy.parse_command('@agy review', False)[0]
    assert agy.parse_command('@agy review #12 check tests', False) == (True, 12, 'check tests')


def test_focus_is_single_line_and_capped():
    ok, _, focus = agy.parse_command('@agy review ' + 'a;$(rm -rf /)`\t' * 200, True)
    assert ok and len(focus) <= 500 and '\n' not in focus and '\t' not in focus


def test_authorization():
    assert agy.is_authorized('OWNER', 'User', 'a', '')
    assert not agy.is_authorized('NONE', 'User', 'a', '')
    assert not agy.is_authorized('NONE', 'Bot', 'x[bot]', '')
    assert agy.is_authorized('NONE', 'Bot', 'X[bot]', 'x[bot],y[bot]')
    assert not agy.is_authorized('NONE', 'Bot', 'x[bot]', 'x*')
    assert not agy.is_authorized('OWNER', 'Organization', 'o', '')


def test_linked_issues():
    assert agy.linked_issues('Closes #4, fixes #9 and resolved: #4', 7) == [4, 7, 9]


def test_template_placeholders_all_filled(tmp_path):
    tpl = (pathlib.Path(_p).parents[1] / 'agy/review-template.md').read_text()
    pr = {'number': 5, 'title': 't', 'body': 'b', 'head': {'sha': 'h' * 40}, 'base': {'sha': 'b' * 40}}
    out = agy.build_prompt(tpl, 'o/r', pr, [{'number': 4, 'title': 'i', 'body': 'x'}], [], [], '', str(tmp_path / 'd'))
    assert '{{' not in out and 'h' * 40 in out and 'Verdict: Accepted' in out


def test_runner_passes_prompt_as_single_argv_without_shell_evaluation(tmp_path):
    import os
    import subprocess
    fake = tmp_path / 'agy'
    fake.write_text('#!/usr/bin/env bash\nprintf "%s" "$2" > "$OUT"\n[ "$1" = "-p" ] && [ "$#" = 2 ]\n')
    fake.chmod(0o755)
    prompt = tmp_path / 'prompt.txt'
    evil = 'x; touch PWNED; $(touch PWNED2) `touch PWNED3` "quote" \'single\''
    prompt.write_text(evil)
    env = dict(os.environ, PATH=f'{tmp_path}:{os.environ["PATH"]}', AGY_PROMPT_FILE=str(prompt), OUT=str(tmp_path / 'out'))
    runner = _p.parent / 'agy_run.sh'
    subprocess.run([str(runner)], cwd=tmp_path, env=env, check=True)
    assert (tmp_path / 'out').read_text() == evil
    assert not list(tmp_path.glob('PWNED*'))
