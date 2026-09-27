"""Network settings must reach the actual policy, including offline CLI plans."""
import json
import ipaddress
from pathlib import Path

import pytest
import yaml

from podgrove import cli
from podgrove.compose import Compose
from podgrove.config import load_cluster, load_config
from podgrove.errors import PodgroveError
from podgrove.network import network_settings


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'compose.yaml').write_text('services:\n  app:\n    image: alpine:3.22\n')
    return tmp_path


@pytest.mark.parametrize('loader', [load_cluster, load_config])
@pytest.mark.parametrize('network', [
    {'blocked_cidrs': ['11.2.3.4/24']}, {'blocked_cidrs': ['11.0.0.1']},
    {'blocked_cidrs': ['not-a-network']}, {'blocked_cidrs': ['10.0.0.0/33']},
    {'blocked_cidrs': ['2001:db8::/129']}, {'blocked_cidrs': ['fe80::%eth0/64']},
    {'blocked_cidrs': ['10.0.0.0/8', '10.0.0.0/8']},
    {'blocked_cidrs': ['2001:db8::/32', '2001:0db8::/32']},
    {'blocked_cidrs': '10.0.0.0/8'}, {'blocked_cidrs': [None]},
    {'blocked_cidrs': [1]}, {'blocked_cidrs': [True]},
    {'blocked_cidrs': ['10.0.0.0/8'] * 129},
    {'allow_all': True}, None,
])
def test_invalid_network_stops_both_target_and_compose_config_loads(project, loader, network):
    (project / 'podgrove.yml').write_text(yaml.safe_dump({'network': network}))
    with pytest.raises(PodgroveError, match='network'):
        loader(project)


def test_network_defaults_are_private_and_canonical(project):
    first, second = load_config(project), load_config(project)
    first.network['blocked_cidrs'].append('11.0.0.0/8')
    assert second.network == {'blocked_cidrs': []}
    (project / 'podgrove.yml').write_text('network:\n  blocked_cidrs: ["2001:0DB8::/32", "11.0.0.0/8"]\n')
    assert load_config(project).network == {'blocked_cidrs': ['2001:db8::/32', '11.0.0.0/8']}


@pytest.mark.parametrize('network', [True, [], {'blocked_cidrs': '11.0.0.0/8'}, {'unknown': []}])
def test_direct_network_builder_rejects_invalid_shapes(network):
    with pytest.raises(PodgroveError, match='network'):
        network_settings(network)


@pytest.mark.parametrize('mode', ['shared', 'worktree'])
def test_cli_dry_run_uses_configured_public_cluster_exclusion(project, monkeypatch, capsys, mode):
    (project / 'podgrove.yml').write_text(yaml.safe_dump({
        'cluster': {'namespace': 'team-dev', 'namespace_mode': mode},
        'network': {'blocked_cidrs': ['11.0.0.0/8']},
    }))
    monkeypatch.setattr(Compose, 'model', lambda self: {'services': {'app': {'image': 'alpine:3.22'}}})
    monkeypatch.setattr(cli, 'Kube', lambda *a, **kw: pytest.fail('Dry-run must not contact a cluster'))
    args = cli.parser().parse_args(['up', '--project-directory', str(project), '--dry-run', '--json'])
    assert cli.execute(args) == 0
    rendered = json.loads(capsys.readouterr().out)
    policy, = [item for item in rendered['resources'] if item['kind'] == 'NetworkPolicy']
    assert policy['metadata']['namespace'] == rendered['namespace']
    excluded = [ipaddress.ip_network(cidr) for cidr in policy['spec']['egress'][1]['to'][0]['ipBlock']['except']
    ]
    assert any(ipaddress.ip_network('11.0.0.0/8').subnet_of(cidr) for cidr in excluded)
    assert rendered['namespace_mode'] == mode


def test_invalid_network_is_refused_before_bootstrap_output_or_external_tools(project, monkeypatch):
    (project / 'podgrove.yml').write_text(yaml.safe_dump({
        'cluster': {'context': 'offline', 'namespace': 'team-dev', 'storage_class': 'dynamic'},
        'network': {'blocked_cidrs': ['11.0.0.1/8']},
    }))
    monkeypatch.setattr(cli, 'Kube', lambda *a, **kw: pytest.fail('Invalid network must not contact a cluster'))
    output = project / 'not-created'
    args = cli.parser().parse_args(['bootstrap', '--project-directory', str(project), '--output', str(output)])
    with pytest.raises(PodgroveError, match='network.blocked_cidrs'):
        cli.execute(args)
    assert not Path(output).exists()
