from types import SimpleNamespace

from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import LaunchRequest, launch_primary
from meridian.lib.launch import context as launch_context


def _capture_launch_request(monkeypatch):
    captured = {}
    prepared_policy = SimpleNamespace(tag="policy")
    prepared = SimpleNamespace()

    def fake_compile_prepared_policy_surface(**kwargs):
        captured['request'] = kwargs['request']
        return prepared_policy

    def fake_prepare_launch_surface(**kwargs):
        assert kwargs["prepared_policy"] is prepared_policy
        return prepared

    def fake_bind_launch_context(**kwargs):
        assert kwargs['prepared'] is prepared
        return SimpleNamespace(warnings=(), argv=('fake-harness',))

    monkeypatch.setattr(
        launch_context,
        'compile_prepared_policy_surface',
        fake_compile_prepared_policy_surface,
    )
    monkeypatch.setattr(launch_context, 'prepare_launch_surface', fake_prepare_launch_surface)
    monkeypatch.setattr(launch_context, 'bind_launch_context', fake_bind_launch_context)
    return captured


def test_launch_primary_loads_bootstrap_docs_from_launch_project_root(tmp_path, monkeypatch):
    project_root = tmp_path / 'project'
    (project_root / '.meridian').mkdir(parents=True)
    (project_root / 'mars.toml').write_text('[settings]\ntargets=[".agents"]\n', encoding='utf-8')
    doc_dir = project_root / '.mars' / 'bootstrap' / 'setup'
    doc_dir.mkdir(parents=True)
    (doc_dir / 'BOOTSTRAP.md').write_text('setup docs', encoding='utf-8')

    captured = _capture_launch_request(monkeypatch)

    result = launch_primary(
        project_root=project_root,
        request=LaunchRequest(dry_run=True, include_bootstrap_documents=True),
        harness_registry=get_default_harness_registry(),
    )

    assert result.command == ('fake-harness',)
    docs = captured['request'].supplemental_prompt_documents
    assert [(doc.kind, doc.logical_name) for doc in docs] == [('bootstrap', 'setup')]
    assert docs[0].content == '# Bootstrap: setup (package)\n\nsetup docs'


def test_launch_primary_aggregates_bootstrap_docs_with_skill_tier_first_and_sorted_names(
    tmp_path, monkeypatch
):
    project_root = tmp_path / 'project'
    (project_root / '.meridian').mkdir(parents=True)
    (project_root / 'mars.toml').write_text('[settings]\ntargets=[".agents"]\n', encoding='utf-8')

    alpha_skill_doc = (
        project_root / '.mars' / 'skills' / 'alpha-skill' / 'resources' / 'BOOTSTRAP.md'
    )
    alpha_skill_doc.parent.mkdir(parents=True)
    alpha_skill_doc.write_text('alpha skill docs', encoding='utf-8')

    zeta_skill_doc = project_root / '.mars' / 'skills' / 'zeta-skill' / 'resources' / 'BOOTSTRAP.md'
    zeta_skill_doc.parent.mkdir(parents=True)
    zeta_skill_doc.write_text('zeta skill docs', encoding='utf-8')

    alpha_package_doc = project_root / '.mars' / 'bootstrap' / 'alpha-package' / 'BOOTSTRAP.md'
    alpha_package_doc.parent.mkdir(parents=True)
    alpha_package_doc.write_text('alpha package docs', encoding='utf-8')

    zeta_package_doc = project_root / '.mars' / 'bootstrap' / 'zeta-package' / 'BOOTSTRAP.md'
    zeta_package_doc.parent.mkdir(parents=True)
    zeta_package_doc.write_text('zeta package docs', encoding='utf-8')

    captured = _capture_launch_request(monkeypatch)

    launch_primary(
        project_root=project_root,
        request=LaunchRequest(dry_run=True, include_bootstrap_documents=True),
        harness_registry=get_default_harness_registry(),
    )

    docs = captured['request'].supplemental_prompt_documents
    assert [(doc.kind, doc.logical_name) for doc in docs] == [
        ('bootstrap', 'alpha-skill'),
        ('bootstrap', 'zeta-skill'),
        ('bootstrap', 'alpha-package'),
        ('bootstrap', 'zeta-package'),
    ]


def test_launch_primary_preserves_bootstrap_doc_attribution_for_skill_and_package_docs(
    tmp_path, monkeypatch
):
    project_root = tmp_path / 'project'
    (project_root / '.meridian').mkdir(parents=True)
    (project_root / 'mars.toml').write_text('[settings]\ntargets=[".agents"]\n', encoding='utf-8')

    skill_doc = project_root / '.mars' / 'skills' / 'image-tool' / 'resources' / 'BOOTSTRAP.md'
    skill_doc.parent.mkdir(parents=True)
    skill_doc.write_text('enable image generation', encoding='utf-8')

    package_doc = project_root / '.mars' / 'bootstrap' / 'global-auth' / 'BOOTSTRAP.md'
    package_doc.parent.mkdir(parents=True)
    package_doc.write_text('sign in first', encoding='utf-8')

    captured = _capture_launch_request(monkeypatch)

    launch_primary(
        project_root=project_root,
        request=LaunchRequest(dry_run=True, include_bootstrap_documents=True),
        harness_registry=get_default_harness_registry(),
    )

    docs = captured['request'].supplemental_prompt_documents
    assert [doc.content for doc in docs] == [
        '# Bootstrap: image-tool\n\nenable image generation',
        '# Bootstrap: global-auth (package)\n\nsign in first',
    ]


def test_launch_primary_passes_empty_bootstrap_documents_when_none_exist(tmp_path, monkeypatch):
    project_root = tmp_path / 'project'
    (project_root / '.meridian').mkdir(parents=True)
    (project_root / 'mars.toml').write_text('[settings]\ntargets=[".agents"]\n', encoding='utf-8')

    captured = _capture_launch_request(monkeypatch)

    result = launch_primary(
        project_root=project_root,
        request=LaunchRequest(dry_run=True, include_bootstrap_documents=True),
        harness_registry=get_default_harness_registry(),
    )

    assert result.command == ('fake-harness',)
    assert captured['request'].supplemental_prompt_documents == ()


def test_launch_primary_dry_run_uses_explicit_work_for_prompt_and_env_preview(
    tmp_path, monkeypatch
):
    project_root = tmp_path / 'project'
    runtime_root = project_root / '.meridian'
    (runtime_root / 'work' / 'work-inherited').mkdir(parents=True)
    (project_root / 'mars.toml').write_text('[settings]\ntargets=[".agents"]\n', encoding='utf-8')

    monkeypatch.setenv('MERIDIAN_ACTIVE_WORK_ID', 'work-inherited')
    monkeypatch.setenv(
        'MERIDIAN_ACTIVE_WORK_DIR',
        (runtime_root / 'work' / 'work-inherited').as_posix(),
    )

    captured_active_work_dirs: list[object] = []
    captured_env_overrides: dict[str, str] = {}
    original_build_launch_context_documents = launch_context.build_launch_context_documents
    original_bind_launch_context = launch_context.bind_launch_context

    def fake_build_launch_context_documents(**kwargs):
        captured_active_work_dirs.append(kwargs['active_work_dir'])
        return original_build_launch_context_documents(**kwargs)

    def capture_bind_launch_context(**kwargs):
        bound = original_bind_launch_context(**kwargs)
        captured_env_overrides.update(bound.env_overrides)
        return bound

    monkeypatch.setattr(
        launch_context,
        'build_launch_context_documents',
        fake_build_launch_context_documents,
    )
    monkeypatch.setattr(launch_context, 'bind_launch_context', capture_bind_launch_context)

    launch_primary(
        project_root=project_root,
        request=LaunchRequest(dry_run=True, work_id='Preview Work'),
        harness_registry=get_default_harness_registry(),
    )

    expected_work_dir = runtime_root / 'work' / 'preview-work'
    assert captured_active_work_dirs == [expected_work_dir]
    assert captured_env_overrides['MERIDIAN_ACTIVE_WORK_ID'] == 'preview-work'
    assert captured_env_overrides['MERIDIAN_ACTIVE_WORK_DIR'] == expected_work_dir.as_posix()


def test_launch_primary_prepares_once_binds_preview_once_and_passes_prepared_surface_to_runner(
    tmp_path,
    monkeypatch,
):
    project_root = tmp_path / 'project'
    (project_root / '.meridian').mkdir(parents=True)
    (project_root / 'mars.toml').write_text('[settings]\ntargets=[".agents"]\n', encoding='utf-8')

    prepared_policy = SimpleNamespace(tag='policy')
    prepared = SimpleNamespace(tag='prepared')
    preview_context = SimpleNamespace(warnings=(), argv=('preview-harness',))
    calls: list[str] = []
    runner_args: dict[str, object] = {}

    def fake_compile_prepared_policy_surface(**_kwargs):
        calls.append('compile')
        return prepared_policy

    def fake_prepare_launch_surface(**kwargs):
        assert kwargs['prepared_policy'] is prepared_policy
        calls.append('prepare')
        return prepared

    def fake_bind_launch_context(**kwargs):
        assert kwargs['prepared'] is prepared
        calls.append('bind')
        return preview_context

    def fake_run_harness_process(launch_context, harness_registry, *, prepared=None):
        _ = harness_registry
        runner_args['launch_context'] = launch_context
        runner_args['prepared'] = prepared
        return SimpleNamespace(
            command=('runtime-harness',),
            exit_code=0,
            resolved_harness_session_id='session-1',
            chat_id=None,
        )

    monkeypatch.setattr(
        launch_context,
        'compile_prepared_policy_surface',
        fake_compile_prepared_policy_surface,
    )
    monkeypatch.setattr(launch_context, 'prepare_launch_surface', fake_prepare_launch_surface)
    monkeypatch.setattr(launch_context, 'bind_launch_context', fake_bind_launch_context)
    monkeypatch.setattr('meridian.lib.launch.process.run_harness_process', fake_run_harness_process)

    result = launch_primary(
        project_root=project_root,
        request=LaunchRequest(dry_run=False),
        harness_registry=get_default_harness_registry(),
    )

    assert calls == ['compile', 'prepare', 'bind']
    assert runner_args == {
        'launch_context': preview_context,
        'prepared': prepared,
    }
    assert result.command == ('runtime-harness',)
    assert result.continue_ref == 'session-1'
