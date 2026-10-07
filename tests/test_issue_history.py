from toolforge_app.issues import annotate_report


def evidence(at, line=1):
    return dict(latest_check=at, manual=[dict(title='Пример', reason='Конфликт шаблонов',
                                           verified_at=at, line=line)])


def test_confirmation_uses_observation_time_and_identity_survives_line_shifts(store):
    first = annotate_report(store, 'sections-empty-to-fill', evidence(100))['manual'][0]
    repeated = annotate_report(store, 'sections-empty-to-fill', evidence(100, 20))['manual'][0]
    assert first['issue_key'] == repeated['issue_key']
    assert repeated['problem_history'] == dict(first_seen=100, last_seen=100, episodes=1)
    new = annotate_report(store, 'sections-empty-to-fill', evidence(200))['manual'][0]
    assert new['problem_history'] == dict(first_seen=100, last_seen=200, episodes=1)


def test_problem_resolves_and_recurrence_preserves_first_detection(store):
    first = annotate_report(store, 'translations-talk', evidence(100))['manual'][0]
    annotate_report(store, 'translations-talk', dict(latest_check=200, manual=[]))
    records = store.observe_problems('translations-talk', [], {})
    assert records[first['issue_key']]['resolved_at'] == 200
    # Rendering an earlier cached report cannot resurrect the finding.
    annotate_report(store, 'translations-talk', evidence(150))
    assert store.observe_problems('translations-talk', [], {})[first['issue_key']]['resolved_at'] == 200
    new = annotate_report(store, 'translations-talk', evidence(300))['manual'][0]
    assert new['problem_history'] == dict(first_seen=100, last_seen=300, episodes=2)


def test_refreshing_inventory_does_not_claim_to_recheck_translation_source(store):
    annotate_report(store, 'translations-talk', evidence(100))
    cached = evidence(100)
    cached['latest_check'] = 900
    result = annotate_report(store, 'translations-talk', cached)
    assert result['manual'][0]['problem_history']['last_seen'] == 100


def test_old_absent_snapshot_does_not_resolve_newer_problem(store):
    first = annotate_report(store, 'maintenance-rq', evidence(300))['manual'][0]
    annotate_report(store, 'maintenance-rq', dict(latest_check=200, manual=[]))
    assert store.observe_problems('maintenance-rq', [], {})[first['issue_key']]['resolved_at'] is None


def test_other_page_observations_cannot_resolve_a_problem_on_unchecked_page(store):
    store.observe_problems('obkat', [dict(issue_key='problem', scope='Page A', at=300)], {'Page A': 300})
    records = store.observe_problems('obkat', [], {'Page A': 200, 'Page B': 400})
    assert records['problem']['resolved_at'] is None
    assert store.observe_problems('obkat', [], {'Page A': 500})['problem']['resolved_at'] == 500
