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
from podgrove.network import network_settings, validate_model


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


INVALID_MODE_SETTINGS = [
    {'pod_to_pod': None}, {'pod_to_pod': True}, {'pod_to_pod': 'all'},
    {'expose': []}, {'connect': []}, {'pod_to_pod': 'disabled', 'expose': []},
    {'pod_to_pod': 'open', 'connect': []},
    {'pod_to_pod': 'selected', 'expose': [{'service': 'api'}]},
    {'pod_to_pod': 'selected', 'expose': [{'service': 'api', 'from': []}]},
    {'pod_to_pod': 'selected', 'expose': [{'service': 'api', 'from': [{}]}]},
    {'pod_to_pod': 'selected', 'expose': [{'service': 'api', 'from': [{'namespace': '*'}]}]},
    {'pod_to_pod': 'selected', 'connect': [{'namespace': 'team', 'ports': [80]}]},
    {'pod_to_pod': 'selected', 'connect': [{'namespace': 'team', 'worktree': '*', 'ports': []}]},
    {'pod_to_pod': 'selected', 'connect': [{'namespace': 'team', 'worktree': '*', 'ports': [80, 80]}]},
    {'pod_to_pod': 'selected', 'connect': [{'namespace': 'team', 'worktree': '*', 'ports': [80], 'context': 'other'}]},
]
INVALID_MODE_SETTINGS += [
    {'pod_to_pod': 'selected', 'connect': [{'namespace': 'team', 'worktree': '*', 'ports': [port]}]}
    for port in (0, 65536, 2375, 2376, True, '80', 80.0)
]
INVALID_MODE_SETTINGS += [
    {'pod_to_pod': 'selected', 'connect': [{'namespace': 'team', 'worktree': pattern, 'ports': [80]}]}
    for pattern in ('', ' ', '.', '..', 'api/*', 'api\\*', 'api\n*', 'api\n', '*' * 129)
]
INVALID_MODE_SETTINGS.append({'pod_to_pod': 'selected', 'connect': [
    {'namespace': 'team\n', 'worktree': '*', 'ports': [80]},
]})


@pytest.mark.parametrize('loader', [load_cluster, load_config])
@pytest.mark.parametrize('network', INVALID_MODE_SETTINGS)
def test_network_modes_reject_invalid_intent_before_compose_or_cluster(project, loader, network):
    (project / 'podgrove.yml').write_text(yaml.safe_dump({'network': network}))
    with pytest.raises(PodgroveError, match='network'):
        loader(project)


@pytest.mark.parametrize('mode', [None, 'disabled'])
def test_disabled_mode_preserves_the_persisted_legacy_network_shape(mode):
    configured = {} if mode is None else {'pod_to_pod': mode}
    assert network_settings(configured) == {'blocked_cidrs': []}


def test_selected_rules_are_normalized_without_mutating_or_sharing_user_data():
    configured = {'pod_to_pod': 'selected', 'expose': [{'service': 'api', 'from': [{'namespace': 'team'}]}],
                  'connect': [{'namespace': 'other-team', 'worktree': 'apis-[ab]?', 'ports': [8080]}]}
    first, second = network_settings(configured), network_settings(configured)
    first['connect'][0]['ports'].append(8443)
    assert configured['connect'][0]['ports'] == second['connect'][0]['ports'] == [8080]
    assert second['expose'][0]['from'] == [{'namespace': 'team'}]
    assert second['blocked_cidrs'] == []


def test_duplicate_exposure_service_requires_combining_its_peer_rules():
    config = {'pod_to_pod': 'selected', 'expose': [
        {'service': 'api', 'from': [{'namespace': 'team-a'}]},
        {'service': 'api', 'from': [{'namespace': 'team-b'}]},
    ]}
    with pytest.raises(PodgroveError, match='duplicate service'):
        network_settings(config)


@pytest.fixture
def selected_network():
    return {'pod_to_pod': 'selected', 'expose': [{'service': 'api', 'from': [{'namespace': 'team', 'worktree': 'web-*'}]}]}


def test_expose_model_resolves_published_not_container_ports_offline(selected_network):
    model = {'services': {'api': {'ports': [{'target': 8080, 'published': '18080'},
                                          {'target': 8443, 'published': 18443, 'protocol': 'tcp', 'host_ip': '0.0.0.0'}]},
                          'private': {'ports': [{'target': 9000}]}}}
    assert validate_model(selected_network, model) == [
        {'service': 'api', 'target': 8080, 'published': 18080},
        {'service': 'api', 'target': 8443, 'published': 18443},
    ]
    assert model['services']['api']['ports'][0]['published'] == '18080'


INVALID_EXPOSED_SERVICES = [
    ({}, 'publish at least one'), ({'expose': ['8080']}, 'publish at least one'),
    ({'ports': [{'target': 8080}]}, 'stable published'),
    ({'ports': [{'target': 8080, 'published': 18080, 'protocol': 'udp'}]}, '0.0.0.0 TCP'),
    ({'ports': [{'target': 8080, 'published': 18080}, {'target': 8080, 'published': 18081}]}, 'ambiguous'),
    ({'scale': 2, 'ports': [{'target': 8080, 'published': 18080}]}, 'one service replica'),
    ({'deploy': {'replicas': 0}, 'ports': [{'target': 8080, 'published': 18080}]}, 'one service replica'),
]
INVALID_EXPOSED_SERVICES += [
    ({'ports': [{'target': 8080, 'published': value}]}, 'stable published')
    for value in (0, '0', '18080-18090', -1, 65536, 'junk', None, True, 80.0)
]
INVALID_EXPOSED_SERVICES += [
    ({'ports': [{'target': 8080, 'published': 18080, 'host_ip': value}]}, '0.0.0.0 TCP')
    for value in ('127.0.0.1', '::', '::1', '192.168.1.2')
]
INVALID_EXPOSED_SERVICES += [
    ({'ports': [{'target': 8080, 'published': value}]}, 'reserved') for value in (2375, 2376)
]


@pytest.mark.parametrize('service,match', INVALID_EXPOSED_SERVICES)
def test_invalid_local_exposure_refused_without_remote_discovery(selected_network, service, match):
    with pytest.raises(PodgroveError, match=match):
        validate_model(selected_network, {'services': {'api': service}})


def test_inactive_profile_service_cannot_be_exposed(selected_network):
    with pytest.raises(PodgroveError, match='not active'):
        validate_model(selected_network, {'services': {'worker': {}}})


@pytest.mark.parametrize('settings', [None, {'pod_to_pod': 'disabled'}, {'pod_to_pod': 'open'}])
def test_exposure_restrictions_do_not_change_other_modes_compose_ports(settings):
    assert validate_model(settings, {'services': {'api': {'ports': [{'target': 8080}]}}}) == []


def test_exposure_port_inventory_is_bounded(selected_network):
    ports = [{'target': index + 10000, 'published': index + 20000} for index in range(129)]
    with pytest.raises(PodgroveError, match='at most 128'):
        validate_model(selected_network, {'services': {'api': {'ports': ports}}})


def test_peer_namespace_bound_applies_across_connect_and_expose():
    configured = {'pod_to_pod': 'selected', 'expose': [
        {'service': 'api', 'from': [{'namespace': f'team-{index}'} for index in range(32)]},
    ]}
    assert len(network_settings(configured)['expose'][0]['from']) == 32
    configured['connect'] = [{'namespace': 'one-more', 'worktree': '*', 'ports': [80]}]
    with pytest.raises(PodgroveError, match='at most 32 distinct peer namespaces'):
        network_settings(configured)
