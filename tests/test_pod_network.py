"""Reciprocal worktree rules and bounded namespaced discovery without Kubernetes."""
from copy import deepcopy
import json
from types import SimpleNamespace
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from podgrove.errors import PodgroveError
from podgrove.bootstrap import PROVISIONING_MARKER, provisioning_marker
from podgrove import pod_network as network
from podgrove.network import network_settings, policy_spec
from podgrove.repository import WORKTREE_NAME

SOURCE, TARGET, THIRD = 'a' * 12, 'b' * 12, 'c' * 12
SOURCE_NS, TARGET_NS = 'web-team', 'api-team'
SOURCE_NAME, TARGET_NAME = 'web-main', 'apis-main'
PORTS = [{'service': 'api', 'target': 8080, 'published': 80}]
SOURCE_SETTINGS = {'pod_to_pod': 'selected', 'connect': [
    {'namespace': TARGET_NS, 'worktree': 'apis-*', 'ports': [80]},
]}
TARGET_SETTINGS = {'pod_to_pod': 'selected', 'expose': [
    {'service': 'api', 'from': [{'namespace': SOURCE_NS, 'worktree': 'web-*'}]},
]}


def profile(settings, name, ports=(), uid='pod-uid'):
    return network.declaration(settings, name, deepcopy(list(ports)), uid)


def peer(settings=TARGET_SETTINGS, *, namespace=TARGET_NS, ident=TARGET, name=TARGET_NAME, ports=PORTS):
    return {'namespace': namespace, 'identity': ident, 'declaration': profile(settings, name, ports)}


def rule_peer(namespace, ident, name):
    return {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': namespace}},
            'podSelector': {'matchLabels': {network.MANAGED: 'podgrove', network.ENVIRONMENT: ident,
                                           WORKTREE_NAME: name, 'statefulset.kubernetes.io/pod-name': f'pg-{ident}-0'}}}


def test_reciprocal_connect_and_expose_render_each_engines_own_direction():
    own = profile(SOURCE_SETTINGS, SOURCE_NAME)
    incoming, outgoing, pending = network.selected_rules(SOURCE_NS, SOURCE, own, [peer()])
    assert incoming == pending == []
    assert outgoing == [{'to': [rule_peer(TARGET_NS, TARGET, TARGET_NAME)], 'ports': [{'protocol': 'TCP', 'port': 80}]}]
    own = profile(TARGET_SETTINGS, TARGET_NAME, PORTS)
    incoming, outgoing, pending = network.selected_rules(TARGET_NS, TARGET, own, [
        peer(SOURCE_SETTINGS, namespace=SOURCE_NS, ident=SOURCE, name=SOURCE_NAME, ports=[]),
    ])
    assert outgoing == pending == []
    assert incoming == [{'from': [rule_peer(SOURCE_NS, SOURCE, SOURCE_NAME)], 'ports': [{'protocol': 'TCP', 'port': 80}]}]


@pytest.mark.parametrize('change', ['no-expose', 'wrong-source-namespace', 'wrong-source-name',
                                  'wrong-target-namespace', 'wrong-target-name', 'wrong-port', 'target-open', 'target-disabled'])
def test_no_grant_without_exact_mutual_selected_consent(change):
    remote = peer()
    if change == 'no-expose':
        remote['declaration']['network']['expose'] = []
    elif change == 'wrong-source-namespace':
        remote['declaration']['network']['expose'][0]['from'][0]['namespace'] = 'other'
    elif change == 'wrong-source-name':
        remote['declaration']['network']['expose'][0]['from'][0]['worktree'] = 'Web-*'
    elif change == 'wrong-target-namespace':
        remote['namespace'] = 'other'
    elif change == 'wrong-target-name':
        remote['declaration']['worktree'] = 'other-main'
    elif change == 'wrong-port':
        remote['declaration']['ports'][0]['published'] = 18080
    else:
        remote['declaration']['network'] = network_settings({'pod_to_pod': change.removeprefix('target-')})
    incoming, outgoing, pending = network.selected_rules(SOURCE_NS, SOURCE, profile(SOURCE_SETTINGS, SOURCE_NAME), [remote])
    assert incoming == outgoing == []
    assert pending


def test_missing_peer_stays_pending_without_a_wildcard_allowance():
    incoming, outgoing, pending = network.selected_rules(SOURCE_NS, SOURCE, profile(SOURCE_SETTINGS, SOURCE_NAME), [])
    assert incoming == outgoing == []
    assert pending == [f'{TARGET_NS}/apis-*: no ready selected peer']


def test_omitted_expose_worktree_matches_any_name_only_in_its_exact_namespace():
    settings = deepcopy(TARGET_SETTINGS)
    settings['expose'][0]['from'][0].pop('worktree')
    own = profile(settings, TARGET_NAME, PORTS)
    remote = peer(SOURCE_SETTINGS, namespace=SOURCE_NS, ident=SOURCE, name='unrelated-name', ports=[])
    incoming, _, _ = network.selected_rules(TARGET_NS, TARGET, own, [remote])
    assert len(incoming) == 1
    remote['namespace'] = 'other'
    assert network.selected_rules(TARGET_NS, TARGET, own, [remote])[0] == []


def test_worktree_glob_resolves_multiple_exact_identities_without_broad_selector():
    remote = [peer(), peer(ident=THIRD, name='apis-second')]
    _, outgoing, pending = network.selected_rules(SOURCE_NS, SOURCE, profile(SOURCE_SETTINGS, SOURCE_NAME), remote)
    assert not pending and len(outgoing) == 2
    assert {rule['to'][0]['podSelector']['matchLabels'][network.ENVIRONMENT] for rule in outgoing} == {TARGET, THIRD}
    assert all(rule['to'][0]['namespaceSelector'] == {'matchLabels': {'kubernetes.io/metadata.name': TARGET_NS}}
               for rule in outgoing)


def test_unexposed_service_port_never_satisfies_a_connection():
    remote = peer(ports=[{'service': 'private', 'target': 8080, 'published': 80}])
    assert network.selected_rules(SOURCE_NS, SOURCE, profile(SOURCE_SETTINGS, SOURCE_NAME), [remote])[1] == []


def test_one_connection_can_span_multiple_reciprocally_exposed_services():
    source_settings = deepcopy(SOURCE_SETTINGS)
    source_settings['connect'][0]['ports'] = [80, 443]
    target_settings = deepcopy(TARGET_SETTINGS)
    target_settings['expose'].append({'service': 'admin', 'from': deepcopy(target_settings['expose'][0]['from'])})
    ports = [*PORTS, {'service': 'admin', 'target': 8443, 'published': 443}]
    incoming, _, _ = network.selected_rules(TARGET_NS, TARGET, profile(target_settings, TARGET_NAME, ports), [
        peer(source_settings, namespace=SOURCE_NS, ident=SOURCE, name=SOURCE_NAME, ports=[]),
    ])
    assert incoming == [{'from': [rule_peer(SOURCE_NS, SOURCE, SOURCE_NAME)],
                         'ports': [{'protocol': 'TCP', 'port': 80}, {'protocol': 'TCP', 'port': 443}]}]


def test_requested_missing_port_refuses_the_entire_matching_connection():
    source_settings = deepcopy(SOURCE_SETTINGS)
    source_settings['connect'][0]['ports'] = [80, 443]
    _, outgoing, pending = network.selected_rules(SOURCE_NS, SOURCE, profile(source_settings, SOURCE_NAME), [peer()])
    assert outgoing == [] and '443' in pending[0]


def test_self_peer_never_adds_a_rule_and_duplicate_inventory_does_not_duplicate_rules():
    own = profile(SOURCE_SETTINGS, SOURCE_NAME)
    self_peer = peer(SOURCE_SETTINGS, namespace=SOURCE_NS, ident=SOURCE, name=SOURCE_NAME, ports=[])
    assert network.selected_rules(SOURCE_NS, SOURCE, own, [self_peer, peer(), peer()]) == network.selected_rules(
        SOURCE_NS, SOURCE, own, [peer()])


@pytest.mark.parametrize('field,value', [
    ('version', True), ('version', 2), ('version', '1'), ('network', {'pod_to_pod': 'future'}), ('network', None),
    ('worktree', ''), ('worktree', 'bad/name'), ('worktree', 'name\n'),
    ('ports', None), ('ports', [{}]), ('ports', PORTS * 129),
    ('pod_uid', True), ('pod_uid', 2), ('pod_uid', ''),
])
def test_malformed_peer_declaration_is_refused(field, value):
    value_profile = profile(TARGET_SETTINGS, TARGET_NAME, PORTS)
    value_profile[field] = value
    with pytest.raises(PodgroveError):
        network._checked_declaration(json.dumps(value_profile))


@pytest.mark.parametrize('field,value', [('target', True), ('published', True), ('published', 0),
                                       ('published', 65536), ('published', 2375), ('published', 2376),
                                       ('service', 'unsafe/name')])
def test_peer_published_ports_have_strict_types_and_application_scope(field, value):
    value_profile = profile(TARGET_SETTINGS, TARGET_NAME, PORTS)
    value_profile['ports'][0][field] = value
    with pytest.raises(PodgroveError):
        network._checked_declaration(json.dumps(value_profile))


@pytest.mark.parametrize('payload', ['', '{}', '[]', 'null', '1', 'not-json', ' ' * 32769])
def test_peer_declaration_shape_and_bytes_are_bounded(payload):
    with pytest.raises(PodgroveError):
        network._checked_declaration(payload)


def application_model():
    return {'name': 'apis', 'services': {'api': {'ports': [{'target': 8080, 'published': '80'}]}}}


def application_rows():
    return [{'ID': 'd' * 64, 'Project': 'apis', 'Service': 'api', 'State': 'running',
             'Publishers': [{'TargetPort': 8080, 'PublishedPort': 80, 'Protocol': 'tcp', 'URL': '0.0.0.0'}]}]


def test_live_exposure_requires_observed_exact_project_service_target_and_published_port():
    assert network.observed_ports(network_settings(TARGET_SETTINGS), application_model(), application_rows()) == PORTS


@pytest.mark.parametrize('change', ['wrong-project', 'wrong-service', 'exited', 'loopback', 'wrong-published',
                                  'wrong-target', 'udp', 'bool-port', 'unpublished', 'ambiguous-replicas'])
def test_unproved_or_ambiguous_live_publishers_cannot_receive_ingress(change):
    rows = application_rows()
    row, binding = rows[0], rows[0]['Publishers'][0]
    if change == 'wrong-project':
        row['Project'] = 'other'
    elif change == 'wrong-service':
        row['Service'] = 'other'
    elif change == 'exited':
        row['State'] = 'exited'
    elif change == 'loopback':
        binding['URL'] = '127.0.0.1'
    elif change == 'wrong-published':
        binding['PublishedPort'] = 18080
    elif change == 'wrong-target':
        binding['TargetPort'] = 8081
    elif change == 'udp':
        binding['Protocol'] = 'udp'
    elif change == 'bool-port':
        binding['PublishedPort'] = True
    elif change == 'unpublished':
        row['Publishers'] = []
    else:
        rows.append({**deepcopy(row), 'ID': 'e' * 64})
    with pytest.raises(PodgroveError):
        network.observed_ports(network_settings(TARGET_SETTINGS), application_model(), rows)


def objects(namespace, ident, name, settings, ports):
    labels = {network.MANAGED: 'podgrove', network.ENVIRONMENT: ident, WORKTREE_NAME: name}
    meta = {'namespace': namespace, 'name': f'pg-{ident}-0', 'uid': f'{ident}-pod', 'resourceVersion': '1',
            'labels': labels, 'ownerReferences': [{'kind': 'StatefulSet', 'name': f'pg-{ident}',
                                                 'uid': f'{ident}-controller', 'controller': True}]}
    pod = {'metadata': meta, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
    lease = {'metadata': {'namespace': namespace, 'name': f'pg-{ident}', 'uid': f'{ident}-lease',
                          'resourceVersion': '1', 'labels': deepcopy(labels)},
             'data': {network.DECLARATION: json.dumps(profile(settings, name, ports, meta['uid']))}}
    return pod, lease


class MemoryKube:
    namespace, context = SOURCE_NS, 'offline-test'

    def __init__(self):
        self.inventory = {}
        for args in ((SOURCE_NS, SOURCE, SOURCE_NAME, SOURCE_SETTINGS, []),
                     (TARGET_NS, TARGET, TARGET_NAME, TARGET_SETTINGS, PORTS)):
            pod, lease = objects(*args)
            self.inventory[(args[0], 'pods', pod['metadata']['name'])] = pod
            self.inventory[(args[0], 'configmaps', lease['metadata']['name'])] = lease
        self.calls, self.policies = [], []
        self.inventory[(SOURCE_NS, 'configmaps', PROVISIONING_MARKER)] = provisioning_marker(SOURCE_NS, 'shared')
        self.list_override = None
        self.forbidden = False
        self.allowed_namespaces = {TARGET_NS}

    def get(self, kind, name):
        plural = {'pod': 'pods', 'configmap': 'configmaps', 'NetworkPolicy': 'networkpolicies'}[kind]
        return deepcopy(self.inventory.get((self.namespace, plural, name), {}))

    def call(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if args[:2] == ('get', '--raw'):
            url = urlsplit(args[2])
            fields = url.path.split('/')
            assert fields[:4] == ['', 'api', 'v1', 'namespaces'] and len(fields) == 6
            namespace, kind = fields[4:]
            assert namespace in self.allowed_namespaces and kind in ('pods', 'configmaps')
            query = parse_qs(url.query)
            assert query['labelSelector'] == [f'{network.MANAGED}=podgrove']
            assert query['limit'] == [str(network.MAX_PEERS)]
            if self.forbidden:
                raise PodgroveError('Forbidden: peer namespace read access is not granted')
            result = self.list_override or {'metadata': {}, 'items': [deepcopy(value)
                for (ns, resource, _), value in self.inventory.items() if ns == namespace and resource == kind]}
        elif args[0] == 'get':
            assert args[3:] == ('-o', 'json', '--ignore-not-found')
            result = self.get(args[1], args[2])
        elif args[:2] == ('patch', 'configmap'):
            assert args[2] == f'pg-{SOURCE}' and args[3:5] == ('--type=json', '-p')
            patch = json.loads(args[5])
            lease = self.inventory[(SOURCE_NS, 'configmaps', f'pg-{SOURCE}')]
            assert patch[:2] == [
                {'op': 'test', 'path': '/metadata/uid', 'value': lease['metadata']['uid']},
                {'op': 'test', 'path': '/metadata/resourceVersion', 'value': lease['metadata']['resourceVersion']},
            ]
            assert patch[2]['op'] == 'add' and patch[2]['path'] == '/data/' + network.DECLARATION
            lease['data'][network.DECLARATION] = patch[2]['value']
            result = lease
        elif args[0] in ('create', 'replace'):
            assert args[1:] == ('-f', '-')
            result = json.loads(kwargs['input'])
            assert result['kind'] == 'NetworkPolicy'
            meta = result['metadata']
            assert meta['namespace'] == SOURCE_NS and meta['name'] == f'pg-{SOURCE}'
            key = (SOURCE_NS, 'networkpolicies', meta['name'])
            if args[0] == 'replace':
                previous = self.inventory[key]['metadata']
                assert (meta['uid'], meta['resourceVersion']) == (previous['uid'], previous['resourceVersion'])
            else:
                assert key not in self.inventory
                meta.update(uid='policy-uid', resourceVersion='1')
            self.policies.append(deepcopy(result))
            self.inventory[key] = deepcopy(result)
        else:
            raise AssertionError(args)
        return SimpleNamespace(stdout=json.dumps(result))

@pytest.fixture
def manager():
    kube = MemoryKube()
    instance = network.PodNetwork(kube, SOURCE, SOURCE_NAME, SOURCE_SETTINGS, {'services': {'client': {}}},
                                  expected_uids={'pod_uid': f'{SOURCE}-pod', 'statefulset_uid': f'{SOURCE}-controller'})
    return instance, kube


def test_manager_publishes_uid_fenced_declaration_and_only_own_policy(manager):
    instance, kube = manager
    assert instance.refresh([])['state'] == 'ready'
    assert kube.policies[-1]['spec']['egress'][-1]['ports'] == [{'protocol': 'TCP', 'port': 80}]
    assert all(0 < kwargs.get('timeout') <= 15 and kwargs.get('cancel_event') is instance.stopping
               for _, kwargs in kube.calls)


def test_reconcile_config_removal_withdraws_existing_peer_rule(manager):
    instance, kube = manager
    assert instance.refresh([])['state'] == 'ready'
    instance.settings = network_settings({'pod_to_pod': 'selected'})
    assert instance.refresh()['state'] == 'ready'
    assert kube.policies[-1]['spec'] == policy_spec(SOURCE)


def test_lease_conflict_never_replays_grant_but_allows_distinct_guarded_withdrawal(manager):
    instance, kube = manager
    assert instance.refresh([])['state'] == 'ready'
    lease = kube.inventory[(SOURCE_NS, 'configmaps', f'pg-{SOURCE}')]
    lease['data'].pop(network.DECLARATION)
    original = kube.call
    patches = []
    def conflict(*args, **kwargs):
        if args[:2] == ('patch', 'configmap'):
            patches.append(args)
            raise PodgroveError('Conflict: resourceVersion changed')
        return original(*args, **kwargs)
    kube.call = conflict
    assert instance.refresh()['state'] == 'unavailable'
    assert len(patches) == 2
    profiles = [json.loads(json.loads(args[5])[2]['value']) for args in patches]
    assert profiles[0]['pod_uid'] == f'{SOURCE}-pod'
    assert profiles[1]['pod_uid'] is None and profiles[1]['ports'] == []
    assert kube.policies[-1]['spec'] == policy_spec(SOURCE)


@pytest.mark.parametrize('change', ['missing-pod', 'stale-uid', 'not-ready', 'wrong-name-label', 'wrong-owner'])
def test_stale_or_missing_peer_ownership_withdraws_prior_grant(manager, change):
    instance, kube = manager
    assert instance.refresh([])['state'] == 'ready'
    pod = kube.inventory[(TARGET_NS, 'pods', f'pg-{TARGET}-0')]
    if change == 'missing-pod':
        del kube.inventory[(TARGET_NS, 'pods', f'pg-{TARGET}-0')]
    elif change == 'stale-uid':
        pod['metadata']['uid'] = 'replacement-pod'
    elif change == 'not-ready':
        pod['status']['conditions'][0]['status'] = 'False'
    elif change == 'wrong-name-label':
        pod['metadata']['labels'][WORKTREE_NAME] = 'another-worktree'
    else:
        pod['metadata']['ownerReferences'][0]['controller'] = False
    assert instance.refresh()['state'] == 'waiting'
    assert kube.policies[-1]['spec'] == policy_spec(SOURCE)


def test_peer_namespace_rbac_denial_revokes_instead_of_guessing_or_widening(manager):
    instance, kube = manager
    assert instance.refresh([])['state'] == 'ready'
    kube.forbidden = True
    result = instance.refresh()
    assert result['state'] == 'unavailable' and 'Forbidden' in result['error']
    assert kube.policies[-1]['spec'] == policy_spec(SOURCE)
    saved = json.loads(kube.inventory[(SOURCE_NS, 'configmaps', f'pg-{SOURCE}')]['data'][network.DECLARATION])
    assert saved['pod_uid'] is None and saved['ports'] == []


@pytest.mark.parametrize('response', [
    {'metadata': {'continue': 'more'}, 'items': []},
    {'metadata': {}, 'items': [{}] * 129}, {'metadata': {}, 'items': {}},
])
def test_incomplete_or_unbounded_peer_list_never_creates_a_partial_grant(manager, response):
    instance, kube = manager
    kube.list_override = response
    assert instance.refresh([])['state'] == 'unavailable'
    assert kube.policies[-1]['spec'] == policy_spec(SOURCE)


@pytest.mark.parametrize('resource,field,value', [
    ('pods', 'metadata', []), ('pods', 'status', []),
    ('configmaps', 'data', []), ('configmaps', 'metadata', []),
])
def test_malformed_peer_objects_fail_closed_without_escaping_monitor(manager, resource, field, value):
    instance, kube = manager
    assert instance.refresh([])['state'] == 'ready'
    name = f'pg-{TARGET}-0' if resource == 'pods' else f'pg-{TARGET}'
    kube.inventory[(TARGET_NS, resource, name)][field] = value
    assert instance.refresh()['state'] == 'unavailable'
    assert kube.policies[-1]['spec'] == policy_spec(SOURCE)


@pytest.mark.parametrize('resource', ['pods', 'configmaps'])
def test_own_uid_replacement_is_reported_without_republishing_declaration(manager, resource):
    instance, kube = manager
    assert instance.refresh([])['state'] == 'ready'
    before = len([args for args, _ in kube.calls if args[0] == 'patch'])
    name = f'pg-{SOURCE}-0' if resource == 'pods' else f'pg-{SOURCE}'
    kube.inventory[(SOURCE_NS, resource, name)]['metadata']['uid'] = 'replacement'
    assert instance.refresh()['state'] == 'unavailable'
    assert len([args for args, _ in kube.calls if args[0] == 'patch']) == before


def test_snapshot_is_private_and_close_withdraws_only_this_engine_grants(manager):
    instance, kube = manager
    instance.refresh([])
    snapshot = instance.snapshot()
    snapshot['state'] = 'edited'
    assert instance.snapshot()['state'] == 'ready'
    instance.close()
    assert instance.stopping.is_set()
    assert kube.policies[-1]['spec'] == policy_spec(SOURCE)
    saved = json.loads(kube.inventory[(SOURCE_NS, 'configmaps', f'pg-{SOURCE}')]['data'][network.DECLARATION])
    assert saved['pod_uid'] is None and saved['ports'] == []


def test_pure_renderer_rejects_more_than_global_peer_bound():
    with pytest.raises(PodgroveError, match='128 total peers'):
        network.selected_rules(SOURCE_NS, SOURCE, profile(SOURCE_SETTINGS, SOURCE_NAME), [peer()] * 129)


def test_peer_bound_applies_to_combined_namespaced_discovery(manager):
    instance, kube = manager
    for key in list(kube.inventory):
        if key[0] == TARGET_NS:
            del kube.inventory[key]
    second = 'second-api-team'
    settings = deepcopy(SOURCE_SETTINGS)
    settings['connect'].append({**settings['connect'][0], 'namespace': second})
    instance.settings = network_settings(settings)
    kube.allowed_namespaces.add(second)
    for index in range(129):
        namespace = TARGET_NS if index < 65 else second
        ident = f'{index:012x}'
        pod, lease = objects(namespace, ident, f'apis-{index}', TARGET_SETTINGS, PORTS)
        kube.inventory[(namespace, 'pods', pod['metadata']['name'])] = pod
        kube.inventory[(namespace, 'configmaps', lease['metadata']['name'])] = lease
    result = instance.refresh([])
    assert result['state'] == 'unavailable' and '128 total peers' in result['error']
    assert kube.policies[-1]['spec'] == policy_spec(SOURCE)


def test_total_reconciliation_budget_caps_later_reads_and_refuses_late_grants(manager, monkeypatch):
    instance, kube = manager
    clock, timeouts = [0.0], []
    monkeypatch.setattr(network, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    original = kube.call
    def delayed(*args, **kwargs):
        timeouts.append(kwargs['timeout'])
        clock[0] += min(9, kwargs['timeout'])
        return original(*args, **kwargs)
    kube.call = delayed
    result = instance.refresh([])
    assert result['state'] == 'unavailable' and 'deadline expired' in result['error']
    assert clock[0] <= network.RECONCILE_TIMEOUT
    assert min(timeouts) < 9
    assert not kube.policies


def test_cancellation_stops_blocked_discovery_and_revokes_with_fresh_cleanup_event(manager):
    instance, kube = manager
    assert instance.refresh([])['state'] == 'ready'
    blocked = threading.Event()
    original = kube.call
    errors = []
    def gated(*args, **kwargs):
        if args[:2] == ('get', '--raw'):
            blocked.set()
            if not kwargs['cancel_event'].wait(2):
                raise AssertionError('Discovery did not receive the cancellation event')
            raise PodgroveError('cancelled')
        return original(*args, **kwargs)
    kube.call = gated
    def refresh():
        try:
            instance.refresh()
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=refresh)
    worker.start()
    try:
        assert blocked.wait(2)
        instance.stopping.set()
        worker.join(2)
        assert not worker.is_alive() and not errors
        assert instance.snapshot()['state'] == 'unavailable'
        assert kube.policies[-1]['spec'] == policy_spec(SOURCE)
        assert kube.calls[-1][1]['cancel_event'] is not instance.stopping
    finally:
        instance.stopping.set()
        worker.join(3)


def test_close_refuses_revocation_after_own_controller_identity_changes(manager):
    instance, kube = manager
    assert instance.refresh([])['state'] == 'ready'
    before = deepcopy(kube.policies)
    kube.inventory[(SOURCE_NS, 'pods', f'pg-{SOURCE}-0')]['metadata']['ownerReferences'][0]['uid'] = 'foreign-controller'
    with pytest.raises(PodgroveError, match='engine identity changed'):
        instance.close()
    assert kube.policies == before


def test_open_mode_close_clears_advertisement_without_rewriting_policy(manager):
    instance, kube = manager
    instance.settings = network_settings({'pod_to_pod': 'open'})
    assert instance.refresh([])['state'] == 'ready'
    assert kube.policies == []
    instance.close()
    saved = json.loads(kube.inventory[(SOURCE_NS, 'configmaps', f'pg-{SOURCE}')]['data'][network.DECLARATION])
    assert saved['network']['pod_to_pod'] == 'open' and saved['pod_uid'] is None
    assert kube.policies == []


def test_cancelled_start_never_calls_cluster_or_creates_monitor(manager):
    instance, kube = manager
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(PodgroveError, match='startup cancelled'):
        instance.start([], cancel_event=cancelled)
    assert kube.calls == [] and instance.worker is None


def test_startup_cancel_after_policy_write_revokes_and_does_not_start_monitor(manager):
    instance, kube = manager
    cancelled = threading.Event()
    original = kube.call
    def cancel_after_grant(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[0] == 'create':
            cancelled.set()
        return result
    kube.call = cancel_after_grant
    with pytest.raises(PodgroveError, match='startup cancelled'):
        instance.start([], cancel_event=cancelled)
    assert kube.policies[-1]['spec'] == policy_spec(SOURCE)
    assert instance.worker is None


def test_initial_refresh_respects_shorter_startup_deadline(manager, monkeypatch):
    instance, kube = manager
    clock = [0.0]
    monkeypatch.setattr(network, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    original = kube.call
    def delayed(*args, **kwargs):
        clock[0] += min(3, kwargs['timeout'])
        return original(*args, **kwargs)
    kube.call = delayed
    with pytest.raises(PodgroveError, match='deadline expired'):
        instance.start([], deadline=10)
    assert clock[0] <= 10 and instance.worker is None
    assert not kube.policies


def test_finished_startup_deadline_and_cancel_event_do_not_expire_live_monitor(manager, monkeypatch):
    instance, kube = manager
    clock = [0.0]
    cancelled = threading.Event()
    monkeypatch.setattr(network, 'time', SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(network, 'INTERVAL', 60)
    try:
        instance.start([], deadline=1, cancel_event=cancelled)
        assert instance.snapshot()['state'] == 'ready'
        clock[0] = 5
        cancelled.set()
        assert instance.refresh()['state'] == 'ready'
        assert instance.deadline is None and instance.cancel_event is instance.stopping
    finally:
        instance.close()
    assert not instance.worker.is_alive()
