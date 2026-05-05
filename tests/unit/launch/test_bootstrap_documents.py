from types import SimpleNamespace

from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import LaunchRequest, launch_primary
from meridian.lib.launch import context as launch_context


def _capture_launch_request(monkeypatch):
    captured = {}
    prepared = SimpleNamespace()

    def fake_prepare_launch_surface(**kwargs):
        captured['request'] = kwargs['request']
        return prepared

    def fake_bind_launch_context(**kwargs):
        assert kwargs['prepared'] is prepared
        return SimpleNamespace(warnings=(), argv=('fake-harness',))

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
