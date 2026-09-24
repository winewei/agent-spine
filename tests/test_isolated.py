"""Real Git worktrees and publication races; model processes are deterministic fakes."""
import hashlib
import json
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from npc import isolated as iso, locks, paths, pipeline, state, target


def commit(root, name, text):
    (root / name).write_text(text)
    iso.git(root, 'add', name)
    iso.git(root, 'commit', '-qm', name)
    return iso.head(root)


@pytest.fixture
def run(env_setup, make_args, capsys):
    p = env_setup
    iso.git(p.repo_root, 'checkout', '-qb', 'feature/session-target')
    state.init_run(make_args(plan_order='["alpha", "beta"]'))
    for seq, cid in enumerate(('alpha', 'beta'), 1):
        state.add_change(make_args(seq=seq, change_id=cid, base=None))
    capsys.readouterr()
    return p


def prepare(p, seq, name='feature.txt', text='feature\n'):
    wp = iso.workspace(p, seq)
    tip = commit(wp.repo_root, name, text)
    base = iso.entry(p, seq)['isolation']['base_commit']
    receipt = {'head': tip, 'base_commit': base, 'review_round': 0,
               'patch_sha256': hashlib.sha256(iso.patch(wp.repo_root, base, tip).encode()).hexdigest(),
               'tests': {}}
    iso.update(p, seq, implement_commit=tip, prepared=receipt, status='ready-to-integrate',
               phases={'implement': {'status': 'done', 'commit': tip}},
               last_review={'head': tip, 'blocking': 0, 'round': 0})
    return wp, tip


@pytest.fixture
def archive(monkeypatch):
    calls = []
    def finish(p, seq):
        assert locks.try_acquire(locks.main_lock_path(p.task_log_dir), owner='probe') is None
        calls.append(seq)
        tip = commit(p.repo_root, f'archive-{seq}.txt', 'archived\n')
        iso.update(p, seq, status='archived', archive_commit=tip)
        return {'ok': True, 'seq': seq, 'archive_commit': tip}
    monkeypatch.setattr(pipeline, 'run_archive', finish)
    return calls


def test_publishes_to_session_branch_and_preserves_fix_history(run, archive):
    wp, impl = prepare(run, 1)
    fix = commit(wp.repo_root, 'fix.txt', 'fixed\n')
    base = iso.entry(run, 1)['isolation']['base_commit']
    rec = iso.entry(run, 1)['prepared']
    rec.update(head=fix, patch_sha256=hashlib.sha256(iso.patch(wp.repo_root, base, fix).encode()).hexdigest())
    iso.update(run, 1, prepared=rec)
    out = iso.publish(run, 1)
    assert out['status'] == 'archived'
    assert target.capture(run.repo_root)['target_ref'] == 'refs/heads/feature/session-target'
    assert iso.ancestor(run.repo_root, impl) and iso.ancestor(run.repo_root, fix)
    assert archive == [1]
    assert iso.publish(run, 1)['status'] == 'archived'
    assert archive == [1]


def test_independent_prepared_changes_merge_without_rereview(run, archive):
    a, a_tip = prepare(run, 1, 'a.txt')
    b, b_tip = prepare(run, 2, 'b.txt')
    assert iso.publish(run, 1)['ok']
    assert iso.publish(run, 2)['ok']
    assert iso.ancestor(run.repo_root, a_tip) and iso.ancestor(run.repo_root, b_tip)
    assert (run.repo_root / 'a.txt').exists() and (run.repo_root / 'b.txt').exists()


def test_merge_conflict_leaves_target_and_original_work_intact(run, archive):
    wp, tip = prepare(run, 1, 'README.md', 'ours\n')
    other = commit(run.repo_root, 'README.md', 'theirs\n')
    out = iso.publish(run, 1)
    assert out['status'] == 'needs-resolution'
    assert iso.head(run.repo_root) == other
    assert iso.head(wp.repo_root) == tip
    iso.clean(wp.repo_root)
    assert archive == []


def test_distant_target_edit_in_same_file_keeps_patch_identity(run, archive):
    # Different preimage blob and shifted line numbers, identical hunk content.
    lines = [f'{i}\n' for i in range(40)]
    commit(run.repo_root, 'registry.txt', ''.join(lines))
    ours = list(lines); ours[30] = 'ours\n'
    prepare(run, 1, 'registry.txt', ''.join(ours))
    theirs = ['new\n'] + list(lines); theirs[3] = 'theirs\n'
    commit(run.repo_root, 'registry.txt', ''.join(theirs))
    out = iso.publish(run, 1)
    assert out['status'] == 'archived'
    text = (run.repo_root / 'registry.txt').read_text()
    assert 'ours' in text and 'theirs' in text


def test_changed_hunk_context_requires_delta_review(run, archive):
    lines = [f'{i}\n' for i in range(40)]
    commit(run.repo_root, 'registry.txt', ''.join(lines))
    ours = list(lines); ours[10] = 'ours\n'
    wp, tip = prepare(run, 1, 'registry.txt', ''.join(ours))
    reviewed_base = iso.entry(run, 1)['prepared']['base_commit']
    theirs = list(lines); theirs[12] = 'theirs\n'
    current = commit(run.repo_root, 'registry.txt', ''.join(theirs))
    out = iso.publish(run, 1)
    assert out['status'] == 'needs-review'
    assert iso.head(run.repo_root) == current
    e = iso.entry(run, 1)
    assert e['prepared'] is None
    assert e['isolation']['base_commit'] == current
    assert e['isolation']['reviewed'] == {'base': reviewed_base, 'head': tip, 'derived': []}
    assert 'ours' in (wp.repo_root / 'registry.txt').read_text()
    assert 'theirs' in (wp.repo_root / 'registry.txt').read_text()
    text, _, _ = pipeline._render_focus(wp, 'alpha', 1, tip, base=run.run_dir / 'alpha-art', entry=e)
    assert '## Integration delta review' in text and '## Final isolated patch' not in text
    delta = (run.run_dir / 'alpha-art' / 'round-1.integration-delta.diff').read_text()
    assert '+ theirs' in delta and '- 12' in delta


def _derived_config(p, regenerate):
    cfg = p.repo_root / '.npc' / 'config.toml'
    cfg.parent.mkdir(exist_ok=True)
    cfg.write_text('[integrate]\nderived = ["gen.lock"]\n'
                   f'regenerate = [{json.dumps(regenerate)}]\n')
    iso.git(p.repo_root, 'add', '.npc/config.toml')
    iso.git(p.repo_root, 'commit', '-qm', 'npc config')


REGEN = "python3 -c \"import pathlib; p=pathlib.Path('gen.lock'); p.write_text(''.join(sorted(open(f).read() for f in sorted(pathlib.Path('.').glob('src*.txt')))))\""


def test_derived_only_conflict_is_regenerated_without_review(run, archive):
    _derived_config(run, REGEN)
    commit(run.repo_root, 'gen.lock', 'seed\n')
    wp = iso.workspace(run, 1)
    commit(wp.repo_root, 'src-a.txt', 'a\n')
    prepare(run, 1, 'gen.lock', 'a\n')
    commit(run.repo_root, 'src-b.txt', 'b\n')
    commit(run.repo_root, 'gen.lock', 'b\n')
    out = iso.publish(run, 1)
    assert out['status'] == 'archived'
    assert (run.repo_root / 'gen.lock').read_text() == 'a\nb\n'


def test_source_conflict_is_not_auto_resolved(run, archive):
    _derived_config(run, REGEN)
    commit(run.repo_root, 'gen.lock', 'seed\n')
    wp, tip = prepare(run, 1, 'README.md', 'ours\n')
    commit(wp.repo_root, 'gen.lock', 'ours\n')
    rec = iso.entry(run, 1)['prepared']
    rec.update(head=iso.head(wp.repo_root))
    iso.update(run, 1, prepared={k: v for k, v in rec.items() if k != 'patch_sha256'} | {
        'patch_id': iso.identity(wp.repo_root, rec['base_commit'], rec['head'], ['gen.lock']),
        'derived': ['gen.lock']})
    commit(run.repo_root, 'README.md', 'theirs\n')
    commit(run.repo_root, 'gen.lock', 'theirs\n')
    before = iso.head(wp.repo_root)
    out = iso.publish(run, 1)
    assert out['status'] == 'needs-resolution'
    assert out['reason'] == 'merge-conflict'
    assert sorted(out['conflicts']) == ['README.md', 'gen.lock']
    assert iso.head(wp.repo_root) == before
    iso.clean(wp.repo_root)
    assert iso.entry(run, 1)['isolation']['reviewed']['head'] == before


def test_regenerate_touching_sources_aborts_merge(run, archive):
    _derived_config(run, "python3 -c \"open('gen.lock','w').write('x\\n'); open('README.md','a').write('drift\\n')\"")
    commit(run.repo_root, 'gen.lock', 'seed\n')
    commit(iso.workspace(run, 1).repo_root, 'src-a.txt', 'a\n')
    wp, tip = prepare(run, 1, 'gen.lock', 'ours\n')
    commit(run.repo_root, 'gen.lock', 'theirs\n')
    out = iso.publish(run, 1)
    assert out['status'] == 'needs-resolution'
    assert out['reason'] == 'regenerate-touched-sources'
    assert out['files'] == ['README.md']
    assert iso.head(wp.repo_root) == tip
    iso.clean(wp.repo_root)


def test_tests_run_without_target_lock_and_reject_concurrent_target_advance(run, archive, monkeypatch):
    prepare(run, 1)
    advanced = []
    def test(p):
        lock = locks.try_acquire(locks.main_lock_path(run.task_log_dir), owner='other-publisher')
        assert lock is not None
        try:
            advanced.append(commit(run.repo_root, 'concurrent.txt', 'advance\n'))
        finally:
            locks.release(lock)
        return {'ok': True, 'tests': 'pass'}
    monkeypatch.setattr(iso, 'validate_tests', test)
    assert iso.publish(run, 1)['status'] == 'target-moved'
    assert iso.head(run.repo_root) == advanced[-1]
    assert not (run.repo_root / 'feature.txt').exists()
    assert archive == []


def test_failed_tests_never_touch_target_and_can_retry(run, archive, monkeypatch):
    prepare(run, 1)
    before = iso.head(run.repo_root)
    monkeypatch.setattr(iso, 'validate_tests', lambda p: {'ok': False, 'tests': 'fail'})
    assert iso.publish(run, 1)['status'] == 'tests-failed'
    assert iso.head(run.repo_root) == before
    monkeypatch.setattr(iso, 'validate_tests', lambda p: {'ok': True, 'tests': 'pass'})
    assert iso.publish(run, 1)['ok']


def test_lock_contention_reuses_candidate_tests(run, archive, monkeypatch):
    prepare(run, 1)
    calls = []
    monkeypatch.setattr(iso, 'validate_tests', lambda p: calls.append(p) or {'ok': True})
    lock = locks.try_acquire(locks.main_lock_path(run.task_log_dir), owner='other')
    try:
        assert iso.publish(run, 1)['status'] == 'target-busy'
    finally:
        locks.release(lock)
    assert iso.publish(run, 1)['ok']
    assert len(calls) == 1


def test_branch_switch_is_rejected_even_at_same_commit(run, archive):
    prepare(run, 1)
    iso.git(run.repo_root, 'checkout', '-qb', 'wrong-target')
    before = iso.head(run.repo_root)
    with pytest.raises(ValueError, match='target branch changed'):
        iso.publish(run, 1)
    assert iso.head(run.repo_root) == before
    assert archive == []


def test_resume_does_not_rebind_target(run):
    paths.write_run_json(run)
    iso.git(run.repo_root, 'checkout', '-qb', 'other')
    paths.write_run_json(run)
    assert target.metadata(run)['target_ref'] == 'refs/heads/feature/session-target'


def test_crash_after_publish_resumes_archive_only(run, archive, monkeypatch):
    _, tip = prepare(run, 1)
    real = pipeline.run_archive
    monkeypatch.setattr(pipeline, 'run_archive', lambda *a: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError, match='crash'):
        iso.publish(run, 1)
    assert iso.ancestor(run.repo_root, tip)
    monkeypatch.setattr(pipeline, 'run_archive', real)
    monkeypatch.setattr(iso, 'validate_tests', lambda p: pytest.fail('must reuse published candidate'))
    assert iso.publish(run, 1)['ok']
    assert archive == [1]


def test_two_inner_loops_reach_fix_concurrently_without_target_lock(run, monkeypatch):
    barrier = threading.Barrier(2)
    roots = set()
    def inner(p, seq, **options):
        assert options['defer_archive']
        roots.add(p.repo_root)
        barrier.wait(timeout=10)
        # Simulate a fix phase in both independent worktrees at the same time.
        tip = commit(p.repo_root, f'fix-{seq}.txt', 'fixed\n')
        iso.update(p, seq, last_review={'head': tip, 'blocking': 0, 'round': 1})
        return {'status': 'ready-to-integrate'}
    monkeypatch.setattr(iso.change, 'run_change', inner)
    target_tip = iso.head(run.repo_root)
    # Holding target's lock must not prevent either inner loop from progressing.
    lock = locks.try_acquire(locks.main_lock_path(run.task_log_dir), owner='publisher')
    try:
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(iso.run, run, 1)
            b = pool.submit(iso.run, run, 2)
            assert a.result()['ok'] and b.result()['ok']
    finally:
        locks.release(lock)
    assert len(roots) == 2
    assert iso.head(run.repo_root) == target_tip


def test_prepared_resume_spends_no_model_calls(run, monkeypatch):
    prepare(run, 1)
    monkeypatch.setattr(iso.change, 'run_change', lambda *a, **kw: pytest.fail('duplicate inner loop'))
    assert iso.run(run, 1)['status'] == 'ready-to-integrate'


def test_cannot_prepare_without_clean_review_of_exact_head(run, monkeypatch):
    wp = iso.workspace(run, 1)
    iso.update(run, 1, last_review={'head': iso.head(wp.repo_root), 'blocking': 1, 'round': 0})
    monkeypatch.setattr(iso.change, 'run_change', lambda *a, **kw: {'status': 'ready-to-integrate'})
    assert iso.run(run, 1)['status'] == 'needs-review'
    assert not iso.entry(run, 1).get('prepared')


def test_manual_commit_invalidates_prepared_receipt(run, archive):
    wp, _ = prepare(run, 1)
    commit(wp.repo_root, 'unreviewed.txt', 'new\n')
    before = iso.head(run.repo_root)
    assert iso.publish(run, 1)['status'] == 'needs-review'
    assert iso.head(run.repo_root) == before


def test_native_handoff_implement_review_fix_review_uses_same_worktree(run, monkeypatch, archive):
    reviews = []
    monkeypatch.setattr(pipeline, '_portable_timeout_bin', lambda override=None: Path('/fake/timeout'))
    monkeypatch.setattr(pipeline, '_find_codex_bin', lambda override=None: '/fake/codex')
    def review(**kw):
        reviews.append(kw['repo_root'])
        findings = [] if len(reviews) > 1 else [
            {'id': 'R1', 'severity': 'high', 'in_scope': True, 'category': 'logic',
             'title': 'fix value', 'file': 'feature.txt', 'line_range': '1',
             'detail': 'incorrect value', 'recommendation': 'use fixed value'}]
        kw['review_out'].write_text(json.dumps({'verdict': 'ok', 'findings': findings}))
        kw['events_out'].write_text('')
        return 0
    monkeypatch.setattr(pipeline, '_codex_exec', review)
    initial = iso.head(run.repo_root)
    task = iso.run(run, 1, handoff=True)
    assert task['status'] == 'needs-coder' and task['phase'] == 'implement'
    root = Path(task['worktree'])
    assert iso.run(run, 1, handoff=True) == task  # no duplicate dispatch or prompt rendering
    summary = run.run_dir / 'summary.md'; summary.write_text('done')
    manifest = run.run_dir / 'manifest.json'
    manifest.write_text(json.dumps({'files_written': [{'path': 'feature.txt'}]}))
    receipt = run.run_dir / 'result.txt'
    impl = commit(root, 'feature.txt', 'initial\n')
    receipt.write_text(f'RESULT: commit={impl} tasks=1 tests=pass summary={summary}')
    task = iso.run(run, 1, handoff=True, result_file=str(receipt), manifest=str(manifest))
    assert task['status'] == 'needs-coder' and task['phase'] == 'fix'
    assert task['round'] == 1 and task['worktree'] == str(root)
    assert iso.head(run.repo_root) == initial  # even initial implementation is unpublished
    fixed = commit(root, 'feature.txt', 'fixed\n')
    receipt.write_text(f'RESULT: commit={fixed} fixed=1 tests=pass summary={summary} categories_scanned=logic regressions_added=-')
    out = iso.run(run, 1, handoff=True, result_file=str(receipt), manifest=str(manifest))
    assert out['status'] == 'ready-to-integrate'
    assert reviews == [root, root]
    assert iso.entry(run, 1)['last_review']['head'] == fixed
    assert iso.publish(run, 1)['status'] == 'archived'
    assert (run.repo_root / 'feature.txt').read_text() == 'fixed\n'


def test_interrupted_fix_result_is_recorded_without_another_coder(run, monkeypatch):
    wp, impl = prepare(run, 1)
    iso.update(run, 1, prepared=None, status='in-fix-loop')
    pipeline._do_phase_enter(wp, 1, 'fix-r1')
    fixed = commit(wp.repo_root, 'feature.txt', 'fixed\n')
    base = Path(iso.entry(run, 1)['base'])
    summary = base / 'fix.summary.md'; summary.write_text('fixed')
    (base / 'round-1.fix.result.txt').write_text(
        f'RESULT: commit={fixed} fixed=1 tests=pass summary={summary} categories_scanned=logic regressions_added=-')
    assert iso.recover_coder(wp, 1) is None
    e = iso.entry(run, 1)
    assert e['phases']['fix-r1']['commit'] == fixed
    assert iso.change.derive_start(e, None) == ('review', 1)


def test_unrecorded_commits_require_recovery_instead_of_discard(run):
    wp = iso.workspace(run, 1)
    pipeline._do_phase_enter(wp, 1, 'implement')
    tip = commit(wp.repo_root, 'feature.txt', 'existing work')
    assert iso.run(run, 1)['status'] == 'needs-recovery'
    assert iso.head(wp.repo_root) == tip


def test_isolated_commits_are_not_reported_as_target_drift(run):
    from npc.git_chain import scan_state_drift
    prepare(run, 1)
    assert scan_state_drift(run.repo_root, state.read_state(run.state_json))['total_drifted'] == 0


def test_untracked_project_routing_is_inherited_without_copying_config(run, monkeypatch):
    settings = run.repo_root / '.npc' / 'config.toml'
    settings.parent.mkdir()
    settings.write_text('[verify]\ntest = "false"\n')
    wp = iso.workspace(run, 1)
    assert not (wp.repo_root / '.npc' / 'config.toml').exists()
    out = iso.validate_tests(wp)
    assert out['cmd'] == 'false' and not out['ok']


def test_cli_handoff_returns_structured_task_and_duplicate_worker_is_rejected(run, make_args, capsys):
    args = make_args(seq=1, isolated=True, handoff=True)
    iso.cli_run(run, args)
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'needs-coder'
    assert Path(out['prompt_file']).exists()
    with locks.held(iso.lock_path(run, 1), owner='original-worker', wait_sec=0):
        with pytest.raises(SystemExit) as error:
            iso.cli_run(run, args)
        assert error.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'change-busy'


def test_wrong_native_phase_receipt_is_rejected(run):
    task = iso.run(run, 1, handoff=True)
    root = Path(task['worktree'])
    tip = commit(root, 'feature.txt', 'feature')
    receipt = run.run_dir / 'wrong-result.txt'
    receipt.write_text(f'RESULT: commit={tip} fixed=1 tests=pass summary=-')
    manifest = run.run_dir / 'manifest.json'
    manifest.write_text(json.dumps({'files_written': [{'path': 'feature.txt'}]}))
    with pytest.raises(ValueError, match='missing tasks'):
        iso.run(run, 1, handoff=True, result_file=str(receipt), manifest=str(manifest))
    assert iso.entry(run, 1)['pending_coder']['phase'] == 'implement'


def test_config_error_is_json_not_traceback(run, make_args, capsys):
    args = make_args(seq=1, isolated=True, handoff=True, config=str(run.run_dir / 'missing.toml'))
    with pytest.raises(SystemExit):
        iso.cli_run(run, args)
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'error' and 'missing.toml' in out['reason']


def test_completed_review_resumes_required_fix_not_another_review():
    e = {'status': 'reviewing', 'blocking_trend': [2],
         'phases': {'implement': {'status': 'done'}, 'review-r0': {'status': 'done', 'blocking': 2}}}
    assert iso.change.derive_start(e, None) == ('fix', 1)


def test_old_run_without_recorded_branch_can_use_legacy_but_not_isolated(run):
    state.update_state(run.state_json, run.state_md, lambda st: st.update(target_ref=None))
    target.check(run)
    with pytest.raises(ValueError, match='no recorded target branch'):
        iso.workspace(run, 1)


def test_regenerate_created_files_are_removed_on_abort(run, archive):
    _derived_config(run, "python3 -c \"open('gen.lock','w').write('x\\n'); open('stray.txt','w').write('s\\n')\"")
    commit(run.repo_root, 'gen.lock', 'seed\n')
    commit(iso.workspace(run, 1).repo_root, 'src-a.txt', 'a\n')
    wp, tip = prepare(run, 1, 'gen.lock', 'ours\n')
    commit(run.repo_root, 'gen.lock', 'theirs\n')
    out = iso.publish(run, 1)
    assert out['reason'] == 'regenerate-touched-sources'
    assert out['files'] == ['stray.txt']
    assert 'abort_incomplete' not in out
    assert not (wp.repo_root / 'stray.txt').exists()
    assert iso.head(wp.repo_root) == tip
    iso.clean(wp.repo_root)


def test_regenerate_that_does_not_rebuild_conflict_aborts(run, archive):
    _derived_config(run, "true")
    commit(run.repo_root, 'gen.lock', 'seed\n')
    commit(iso.workspace(run, 1).repo_root, 'src-a.txt', 'a\n')
    wp, tip = prepare(run, 1, 'gen.lock', 'ours\n')
    commit(run.repo_root, 'gen.lock', 'theirs\n')
    out = iso.publish(run, 1)
    assert out['status'] == 'needs-resolution'
    assert out['reason'] == 'regenerate-incomplete'
    assert out['files'] == ['gen.lock']
    assert iso.head(wp.repo_root) == tip
    iso.clean(wp.repo_root)


def test_derived_file_deleted_by_target_is_resolved(run, archive):
    _derived_config(run, REGEN)
    commit(run.repo_root, 'gen.lock', 'seed\n')
    wp = iso.workspace(run, 1)
    commit(wp.repo_root, 'src-a.txt', 'a\n')
    prepare(run, 1, 'gen.lock', 'a\n')
    iso.git(run.repo_root, 'rm', '-q', 'gen.lock')
    iso.git(run.repo_root, 'commit', '-qm', 'drop lock')
    out = iso.publish(run, 1)
    assert out['status'] == 'archived'
    assert (run.repo_root / 'gen.lock').read_text() == 'a\n'


def test_delta_section_falls_back_to_full_patch_without_artifact_dir(run, archive):
    wp, tip = prepare(run, 1)
    e = iso.entry(run, 1)
    e['isolation']['reviewed'] = {'base': e['isolation']['base_commit'], 'head': tip, 'derived': []}
    text, _, _ = pipeline._render_focus(wp, 'alpha', 1, tip, base=None, entry=e)
    assert '## Final isolated patch' in text and '## Integration delta review' not in text
    e['isolation']['reviewed']['head'] = '0' * 40
    text, _, _ = pipeline._render_focus(wp, 'alpha', 1, tip, base=run.run_dir / 'x', entry=e)
    assert '## Final isolated patch' in text


def test_new_format_receipt_publishes_with_pinned_derived(run, archive):
    _derived_config(run, REGEN)
    wp, tip = prepare(run, 1)
    rec = iso.entry(run, 1)['prepared']
    rec.pop('patch_sha256')
    rec.update(patch_id=iso.identity(wp.repo_root, rec['base_commit'], tip, ['gen.lock']), derived=['gen.lock'])
    iso.update(run, 1, prepared=rec)
    commit(run.repo_root, 'other.txt', 'x\n')
    assert iso.publish(run, 1)['status'] == 'archived'


def test_legacy_receipt_of_derived_only_change_keeps_identity_check(run, archive):
    _derived_config(run, REGEN)
    commit(run.repo_root, 'gen.lock', 'seed\n')
    wp, tip = prepare(run, 1, 'gen.lock', 'ours\n')
    receipt = iso.entry(run, 1)['prepared']
    assert iso.receipt_derived(wp.repo_root, receipt, ('gen.lock',)) == ()
    assert iso.receipt_identity(wp.repo_root, receipt, ()) != ''
    commit(run.repo_root, 'gen.lock', 'theirs\n')
    out = iso.publish(run, 1)
    assert out['status'] == 'needs-resolution' and out['reason'] == 'merge-conflict'


def test_both_sides_derived_edit_requires_regeneration(run, archive):
    _derived_config(run, "true")
    commit(run.repo_root, 'gen.lock', ''.join(f'{i}\n' for i in range(30)))
    wp = iso.workspace(run, 1)
    commit(wp.repo_root, 'src-a.txt', 'a\n')
    ours = [f'{i}\n' for i in range(30)]; ours[0] = 'ours\n'
    wp, tip = prepare(run, 1, 'gen.lock', ''.join(ours))
    theirs = [f'{i}\n' for i in range(30)]; theirs[25] = 'theirs\n'
    commit(run.repo_root, 'gen.lock', ''.join(theirs))
    out = iso.publish(run, 1)
    assert out['reason'] == 'regenerate-incomplete' and out['files'] == ['gen.lock']
    assert iso.head(wp.repo_root) == tip
    iso.clean(wp.repo_root)


def test_ignored_artifacts_from_regeneration_are_removed_on_abort(run, archive):
    (run.repo_root / '.gitignore').write_text('dist/\n')
    iso.git(run.repo_root, 'add', '.gitignore')
    iso.git(run.repo_root, 'commit', '-qm', 'ignore')
    _derived_config(run, "python3 -c \"import os; os.makedirs('dist', exist_ok=True); open('dist/out','w').write('x'); exit(1)\"")
    commit(run.repo_root, 'gen.lock', 'seed\n')
    commit(iso.workspace(run, 1).repo_root, 'src-a.txt', 'a\n')
    wp, tip = prepare(run, 1, 'gen.lock', 'ours\n')
    commit(run.repo_root, 'gen.lock', 'theirs\n')
    out = iso.publish(run, 1)
    assert out['reason'] == 'regenerate-failed'
    assert not (wp.repo_root / 'dist').exists()


def test_hook_rewriting_files_reports_exact_paths(run, archive):
    lines = [f'{i}\n' for i in range(40)]
    commit(run.repo_root, 'registry.txt', ''.join(lines))
    wp, tip = prepare(run, 1, 'a.md', 'a\n')
    commit(run.repo_root, 'other.txt', 'x\n')
    hooks = Path(iso.git(wp.repo_root, 'rev-parse', '--path-format=absolute', '--git-path', 'hooks'))
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / 'pre-commit'
    hook.write_text('#!/bin/sh\necho drift >> registry.txt\n')
    hook.chmod(0o755)
    try:
        out = iso.publish(run, 1)
    finally:
        hook.unlink()
    assert out['status'] == 'needs-resolution'
    assert out['reason'] == 'hook-modified-worktree'
    assert out['files'] == ['registry.txt']


def test_abort_keeps_preexisting_ignored_files_when_listing_expands(run, archive):
    (run.repo_root / '.gitignore').write_text('dist/\n')
    iso.git(run.repo_root, 'add', '.gitignore')
    iso.git(run.repo_root, 'commit', '-qm', 'ignore')
    _derived_config(run, "python3 -c 'exit(1)'")
    commit(run.repo_root, 'gen.lock', 'seed\n')
    wp = iso.workspace(run, 1)
    commit(wp.repo_root, 'src-a.txt', 'a\n')
    (wp.repo_root / 'dist').mkdir()
    (wp.repo_root / 'dist' / 'app.bin').write_text('built\n')
    wp, tip = prepare(run, 1, 'gen.lock', 'ours\n')
    # The target starts tracking a file inside the ignored directory.
    (run.repo_root / 'dist').mkdir()
    (run.repo_root / 'dist' / 'manifest.json').write_text('{}\n')
    iso.git(run.repo_root, 'add', '-f', 'dist/manifest.json')
    iso.git(run.repo_root, 'commit', '-qm', 'track manifest')
    commit(run.repo_root, 'gen.lock', 'theirs\n')
    out = iso.publish(run, 1)
    assert out['reason'] == 'regenerate-failed'
    assert (wp.repo_root / 'dist' / 'app.bin').read_text() == 'built\n'
    assert iso.head(wp.repo_root) == tip


def test_target_file_colliding_with_ignored_local_file_is_not_clobbered(run, archive):
    (run.repo_root / '.gitignore').write_text('dist/\n')
    iso.git(run.repo_root, 'add', '.gitignore')
    iso.git(run.repo_root, 'commit', '-qm', 'ignore')
    wp, tip = prepare(run, 1)
    (wp.repo_root / 'dist').mkdir()
    (wp.repo_root / 'dist' / 'app.bin').write_text('PRECIOUS\n')
    (run.repo_root / 'dist').mkdir()
    (run.repo_root / 'dist' / 'app.bin').write_text('tracked\n')
    iso.git(run.repo_root, 'add', '-f', 'dist/app.bin')
    iso.git(run.repo_root, 'commit', '-qm', 'track app.bin')
    out = iso.publish(run, 1)
    assert out['status'] == 'needs-resolution'
    assert out['reason'] == 'merge-would-overwrite-ignored'
    assert out['files'] == ['dist/app.bin']
    assert (wp.repo_root / 'dist' / 'app.bin').read_text() == 'PRECIOUS\n'
    assert iso.head(wp.repo_root) == tip
